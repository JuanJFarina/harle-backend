import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

import harle_api.routes.browser as browser_routes
from harle_api.exception_handlers import (
    register_exception_handlers,
    register_request_id_middleware,
)
from harle_api.routes.browser import (
    get_google_auth_callback,
    post_logout,
)
from harle_api.routes.browser import router as browser_router
from harle_api.settings import ApiSettings
from harle_domain.accounts import (
    BrowserSession,
    GoogleAuthentication,
    IssuedSession,
)
from harle_utils import InvalidOAuthError

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def _settings(
    *,
    same_site: str = "lax",
    secure: bool = True,
) -> ApiSettings:
    return ApiSettings(
        TELEGRAM_BOT_TOKEN="telegram-token",
        TELEGRAM_WEBHOOK_SECRET="telegram-secret",
        POSTGRES_DATABASE_URL="postgresql://user:password@localhost/harle",
        GOOGLE_OAUTH_CLIENT_ID="google-client",
        GOOGLE_OAUTH_CLIENT_SECRET="google-secret",
        GOOGLE_OAUTH_REDIRECT_URI="https://api.test/api/auth/google/callback",
        FRONTEND_REDIRECT_URL="https://app.test",
        FRONTEND_ORIGINS=["https://app.test"],
        SESSION_SIGNING_SECRET="a-session-secret-with-at-least-32-characters",
        SESSION_COOKIE_SECURE=secure,
        SESSION_COOKIE_SAMESITE=cast(object, same_site),
        TELEGRAM_BOT_USERNAME="harle_bot",
        MERCADO_PAGO_ACCESS_TOKEN="payment-token",
        MERCADO_PAGO_WEBHOOK_SECRET="payment-webhook-secret",
        PAYMENT_CHECKOUT_RETURN_URL="https://app.test/suscripcion",
    )


def test_session_cookie_settings_validate_at_construction() -> None:
    assert _settings().SESSION_COOKIE_SAMESITE == "lax"
    cross_site = _settings(same_site="none")
    assert cross_site.SESSION_COOKIE_SAMESITE == "none"
    assert cross_site.SESSION_COOKIE_SECURE
    with pytest.raises(ValidationError):
        _settings(same_site="unsupported")
    with pytest.raises(ValidationError):
        _settings(same_site="none", secure=False)


class FakeGoogleAuth:
    async def complete(
        self,
        *,
        code: str,
        state: str,
        state_cookie: str | None,
    ) -> GoogleAuthentication:
        assert code and state and state_cookie
        session = BrowserSession(
            id=uuid4(),
            user_id=uuid4(),
            expires_at=NOW + timedelta(days=30),
            created_at=NOW,
        )
        return GoogleAuthentication(
            user_id=session.user_id,
            issued_session=IssuedSession(session, "session-token", "csrf-token"),
        )


class FakeSessionService:
    async def resolve(self, token: str | None) -> BrowserSession:
        assert token == "session-token"
        return BrowserSession(uuid4(), uuid4(), NOW + timedelta(days=30), NOW)

    def require_csrf(self, *, session_token: str, csrf_token: str | None) -> None:
        assert session_token == "session-token"
        assert csrf_token == "csrf-token"

    async def revoke(self, token: str | None) -> None:
        assert token == "session-token"


def _request(*, cookie: str, origin: str | None = None) -> Request:
    headers = [(b"cookie", cookie.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("api.test", 443),
            "path": "/api/auth/google/callback",
            "query_string": b"",
            "headers": headers,
        },
    )


def test_login_and_logout_use_the_same_configured_cookie_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def verify() -> None:
        runtime = SimpleNamespace(
            google_auth=FakeGoogleAuth(),
            sessions=FakeSessionService(),
        )
        monkeypatch.setattr(
            browser_routes,
            "get_account_runtime",
            lambda request: runtime,
        )

        default_settings = _settings()
        monkeypatch.setattr(
            browser_routes,
            "get_settings",
            lambda: default_settings,
        )
        default_login = await get_google_auth_callback(
            request=_request(cookie="harle_oauth_state=oauth-state"),
            code="code",
            state="state",
        )
        default_cookie = next(
            value
            for value in default_login.headers.getlist("set-cookie")
            if value.startswith("harle_session=")
        )
        assert "SameSite=lax" in default_cookie

        settings = _settings(same_site="none")
        monkeypatch.setattr(browser_routes, "get_settings", lambda: settings)
        login = await get_google_auth_callback(
            request=_request(cookie="harle_oauth_state=oauth-state"),
            code="code",
            state="state",
        )
        logout = await post_logout(
            request=_request(
                cookie="harle_session=session-token",
                origin="https://app.test",
            ),
            x_csrf_token="csrf-token",
        )

        login_cookie = next(
            value
            for value in login.headers.getlist("set-cookie")
            if value.startswith("harle_session=")
        )
        logout_cookie = next(
            value
            for value in logout.headers.getlist("set-cookie")
            if value.startswith("harle_session=")
        )
        for value in (login_cookie, logout_cookie):
            assert "Path=/api" in value
            assert "SameSite=none" in value
            assert "Secure" in value
        oauth_cookie = next(
            value
            for value in login.headers.getlist("set-cookie")
            if value.startswith("harle_oauth_state=")
        )
        assert "SameSite=lax" in oauth_cookie

    asyncio.run(verify())


class InvalidGoogleAuth:
    def start(self, *, locale: str, timezone_name: str) -> None:
        del locale, timezone_name
        raise InvalidOAuthError


def test_browser_errors_use_request_ids_without_changing_telegram_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    register_exception_handlers(app)
    register_request_id_middleware(app)
    app.include_router(browser_router)

    @app.get("/api/fail")
    async def fail_browser() -> None:
        raise RuntimeError

    @app.get("/telegram/fail")
    async def fail_telegram() -> None:
        raise RuntimeError

    monkeypatch.setattr(browser_routes, "get_settings", _settings)
    monkeypatch.setattr(
        browser_routes,
        "get_account_runtime",
        lambda request: SimpleNamespace(google_auth=InvalidGoogleAuth()),
    )
    client = TestClient(app, raise_server_exceptions=False)

    missing = client.get(
        "/api/auth/google/start",
        headers={"X-Request-ID": "request-123"},
    )
    invalid = client.get(
        "/api/auth/google/start?locale=es-AR&timezone=Invalid",
        headers={"X-Request-ID": "request-456"},
    )
    unexpected = client.get("/api/fail")
    telegram = client.get("/telegram/fail")

    assert missing.json()["error"] == {
        "code": "validation_error",
        "message": "The request is invalid.",
        "fields": [],
        "request_id": "request-123",
    }
    assert missing.headers["X-Request-ID"] == "request-123"
    assert invalid.json()["error"]["request_id"] == "request-456"
    assert invalid.headers["X-Request-ID"] == "request-456"
    assert unexpected.status_code == 500
    assert unexpected.json()["error"]["code"] == "internal_error"
    assert unexpected.json()["error"]["request_id"] == unexpected.headers[
        "X-Request-ID"
    ]
    assert telegram.json() == {"ok": False, "accepted": False}
    assert "error" not in telegram.json()
