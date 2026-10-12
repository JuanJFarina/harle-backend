from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import harle_api.routes.events as event_routes
from harle_api.exception_handlers import (
    register_exception_handlers,
    register_request_id_middleware,
)
from harle_api.routes.events import router as events_router
from harle_api.settings import ApiSettings
from harle_domain.accounts import BrowserSession, SubscriptionPeriod
from harle_domain.events import (
    EventDetails,
    EventInterval,
    EventNotification,
    EventOccurrence,
    EventStatus,
    EventTimestamps,
    EventType,
    InternalEvent,
)
from harle_services.events import NotificationQuotaStatus
from harle_utils import AuthenticationRequiredError, InvalidCsrfError

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
USER_ID = UUID("10000000-0000-0000-0000-000000000001")
EVENT_ID = UUID("20000000-0000-0000-0000-000000000002")


class FakeSessions:
    async def resolve(self, token: str | None) -> BrowserSession:
        if token != "session-token":
            raise AuthenticationRequiredError
        return BrowserSession(uuid4(), USER_ID, NOW + timedelta(days=30), NOW)

    def require_csrf(self, *, session_token: str, csrf_token: str | None) -> None:
        if session_token != "session-token" or csrf_token != "csrf-token":
            raise InvalidCsrfError


class FakeEvents:
    def __init__(self) -> None:
        self.event = _event()

    async def list_for_range(self, **_: object) -> list[InternalEvent]:
        return [
            self.event,
            replace(
                self.event,
                id=uuid4(),
                details=replace(
                    self.event.details,
                    event_type=EventType.SYSTEM_EVENT,
                ),
            ),
        ]

    def occurrences_for_range(self, **_: object) -> list[EventOccurrence]:
        return [EventOccurrence(self.event.id, self.event.details.interval)]

    async def get(self, **_: object) -> InternalEvent | None:
        return self.event

    async def create(self, **_: object) -> InternalEvent:
        return self.event

    async def update(self, **_: object) -> InternalEvent:
        return self.event

    async def disable(self, **_: object) -> InternalEvent:
        self.event = replace(
            self.event,
            details=replace(self.event.details, status=EventStatus.DISABLED),
        )
        return self.event

    async def enable(self, **_: object) -> InternalEvent:
        self.event = replace(
            self.event,
            details=replace(self.event.details, status=EventStatus.ACTIVE),
        )
        return self.event

    async def delete(self, **_: object) -> InternalEvent:
        return self.event


class FakeQuota:
    async def status(self, **_: object) -> NotificationQuotaStatus:
        return NotificationQuotaStatus(60, 42, NOW + timedelta(days=30))


def test_event_list_returns_user_events_occurrences_timezone_and_quota(
    monkeypatch,
) -> None:
    client, _ = _client(monkeypatch)

    response = client.get(
        "/api/events?start_date=2026-10-01&end_date=2026-10-31",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["timezone"] == "America/Argentina/Cordoba"
    assert [event["event_id"] for event in payload["events"]] == [str(EVENT_ID)]
    assert payload["occurrences"][0]["event_id"] == str(EVENT_ID)
    assert payload["notification_quota"]["remaining"] == 42


def test_event_mutations_require_origin_and_csrf(monkeypatch) -> None:
    client, events = _client(monkeypatch)
    body = {
        "title": "Dentist",
        "description": "",
        "starts_at": "2026-10-03T16:30:00",
        "ends_at": "2026-10-03T17:30:00",
        "timezone": "America/Argentina/Cordoba",
        "notify_minutes_before": 60,
        "recurrence_rule": None,
    }

    rejected = client.post(
        "/api/events",
        json=body,
    )
    created = client.post(
        "/api/events",
        json=body,
        headers={"Origin": "https://app.test", "X-CSRF-Token": "csrf-token"},
    )
    disabled = client.post(
        f"/api/events/{EVENT_ID}/disable",
        headers={"Origin": "https://app.test", "X-CSRF-Token": "csrf-token"},
    )

    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "invalid_csrf"
    assert created.status_code == 200
    assert created.json()["operation"] == "created"
    assert disabled.status_code == 200
    assert events.event.status is EventStatus.DISABLED


def test_event_range_is_bounded(monkeypatch) -> None:
    client, _ = _client(monkeypatch)

    response = client.get(
        "/api/events?start_date=2026-01-01&end_date=2026-12-31",
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_event"


def _client(monkeypatch) -> tuple[TestClient, FakeEvents]:
    events = FakeEvents()
    period = SubscriptionPeriod(NOW, NOW + timedelta(days=30))
    account_runtime = SimpleNamespace(
        sessions=FakeSessions(),
        paid_subscriptions=SimpleNamespace(
            overview=_async_result(
                SimpleNamespace(
                    plan=SimpleNamespace(notification_limit=60),
                    period=period,
                ),
            ),
        ),
    )
    runtime = SimpleNamespace(
        profiles=SimpleNamespace(
            get=_async_result(
                SimpleNamespace(timezone="America/Argentina/Cordoba"),
            ),
        ),
        scheduler=SimpleNamespace(
            events=events,
            notifications=SimpleNamespace(quotas=FakeQuota()),
        ),
    )
    monkeypatch.setattr(event_routes, "get_settings", _settings)
    monkeypatch.setattr(
        event_routes,
        "get_account_runtime",
        lambda request: account_runtime,
    )
    monkeypatch.setattr(event_routes, "get_runtime", lambda request: runtime)
    app = FastAPI()
    register_exception_handlers(app)
    register_request_id_middleware(app)
    app.include_router(events_router)
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set("harle_session", "session-token")
    return client, events


def _async_result(value):
    async def result(**_: object):
        return value

    return result


def _settings() -> ApiSettings:
    return ApiSettings(
        TELEGRAM_BOT_TOKEN="telegram-token",
        TELEGRAM_WEBHOOK_SECRET="telegram-secret",
        POSTGRES_DATABASE_URL="postgresql://user:password@localhost/harle",
        GOOGLE_OAUTH_CLIENT_ID="google-client",
        GOOGLE_OAUTH_CLIENT_SECRET="google-secret",
        GOOGLE_OAUTH_REDIRECT_URI="https://app.test/api/auth/google/callback",
        FRONTEND_REDIRECT_URL="https://app.test/registro",
        FRONTEND_ORIGINS=["https://app.test"],
        SESSION_SIGNING_SECRET="a-session-secret-with-at-least-32-characters",
        TELEGRAM_BOT_USERNAME="harle_bot",
        MERCADO_PAGO_ACCESS_TOKEN="payment-token",
        MERCADO_PAGO_WEBHOOK_SECRET="payment-webhook-secret",
        PAYMENT_CHECKOUT_RETURN_URL="https://app.test/suscripcion",
    )


def _event() -> InternalEvent:
    interval = EventInterval(
        starts_at=datetime(2026, 10, 3, 19, 30, tzinfo=timezone.utc),
        ends_at=datetime(2026, 10, 3, 20, 30, tzinfo=timezone.utc),
        timezone="America/Argentina/Cordoba",
        all_day=False,
    )
    return InternalEvent(
        id=EVENT_ID,
        user_id=USER_ID,
        details=EventDetails(
            title="Dentist",
            description="",
            interval=interval,
            event_type=EventType.USER_EVENT,
            status=EventStatus.ACTIVE,
        ),
        notification=EventNotification(
            window_start=interval.starts_at - timedelta(hours=1),
        ),
        timestamps=EventTimestamps(created_at=NOW, updated_at=NOW),
    )
