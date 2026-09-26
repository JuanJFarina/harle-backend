from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from harle_api.dependencies import get_account_runtime
from harle_api.payloads import (
    CheckoutPayload,
    CheckoutRequestPayload,
    PlanPayload,
    SessionPayload,
    SubscriptionPayload,
    TelegramLinkPayload,
)
from harle_api.settings import ApiSettings, get_settings
from harle_utils import InvalidCsrfError

router = APIRouter(prefix="/api")


@router.get("/plans", response_model=list[PlanPayload])
async def get_plans(request: Request) -> list[PlanPayload]:
    plans = await get_account_runtime(request).paid_subscriptions.list_plans()
    return [PlanPayload.from_plan(plan) for plan in plans]


@router.get("/auth/google/start")
async def get_google_auth_start(
    locale: Annotated[str, Query(min_length=2, max_length=32)],
    timezone_name: Annotated[
        str,
        Query(alias="timezone", min_length=1, max_length=128),
    ],
    request: Request,
) -> RedirectResponse:
    settings = get_settings()
    authorization = get_account_runtime(request).google_auth.start(
        locale=locale,
        timezone_name=timezone_name,
    )
    response = RedirectResponse(authorization.url, status_code=302)
    response.set_cookie(
        key=settings.OAUTH_STATE_COOKIE_NAME,
        value=authorization.state_cookie,
        max_age=600,
        secure=settings.SESSION_COOKIE_SECURE,
        httponly=True,
        samesite="lax",
        path="/api/auth/google",
    )
    return response


@router.get("/auth/google/callback")
async def get_google_auth_callback(
    request: Request,
    code: str = "",
    state: str = "",
) -> RedirectResponse:
    settings = get_settings()
    authentication = await get_account_runtime(request).google_auth.complete(
        code=code,
        state=state,
        state_cookie=request.cookies.get(settings.OAUTH_STATE_COOKIE_NAME),
    )
    issued = authentication.issued_session
    response = RedirectResponse(settings.FRONTEND_REDIRECT_URL, status_code=302)
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=issued.token,
        max_age=max(
            0,
            int(
                (
                    issued.session.expires_at - datetime.now(timezone.utc)
                ).total_seconds(),
            ),
        ),
        secure=settings.SESSION_COOKIE_SECURE,
        httponly=True,
        samesite=settings.SESSION_COOKIE_SAMESITE,
        path="/api",
    )
    response.delete_cookie(
        settings.OAUTH_STATE_COOKIE_NAME,
        path="/api/auth/google",
        secure=settings.SESSION_COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/session", response_model=SessionPayload)
async def get_session(request: Request) -> SessionPayload:
    settings = get_settings()
    view = await get_account_runtime(request).accounts.get_session(
        request.cookies.get(settings.SESSION_COOKIE_NAME),
    )
    return SessionPayload.from_view(view)


@router.post("/auth/logout")
async def post_logout(
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> JSONResponse:
    settings = get_settings()
    _require_frontend_origin(request, settings)
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    account_runtime = get_account_runtime(request)
    await account_runtime.sessions.resolve(token)
    account_runtime.sessions.require_csrf(
        session_token=token or "",
        csrf_token=x_csrf_token,
    )
    await account_runtime.sessions.revoke(token)
    response = JSONResponse({"ok": True})
    response.delete_cookie(
        settings.SESSION_COOKIE_NAME,
        path="/api",
        secure=settings.SESSION_COOKIE_SECURE,
        httponly=True,
        samesite=settings.SESSION_COOKIE_SAMESITE,
    )
    return response


@router.get(
    "/account/telegram-link",
    response_model=TelegramLinkPayload,
)
async def get_telegram_link(request: Request) -> TelegramLinkPayload:
    settings = get_settings()
    account_runtime = get_account_runtime(request)
    session = await account_runtime.sessions.resolve(
        request.cookies.get(settings.SESSION_COOKIE_NAME),
    )
    view = await account_runtime.telegram_links.status(user_id=session.user_id)
    return TelegramLinkPayload.from_view(view)


@router.post(
    "/account/telegram-link",
    response_model=TelegramLinkPayload,
)
async def post_telegram_link(
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> TelegramLinkPayload:
    settings = get_settings()
    _require_frontend_origin(request, settings)
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    account_runtime = get_account_runtime(request)
    session = await account_runtime.sessions.resolve(token)
    account_runtime.sessions.require_csrf(
        session_token=token or "",
        csrf_token=x_csrf_token,
    )
    view = await account_runtime.telegram_links.issue(user_id=session.user_id)
    return TelegramLinkPayload.from_view(view)


@router.get("/subscription", response_model=SubscriptionPayload)
async def get_subscription(request: Request) -> SubscriptionPayload:
    settings = get_settings()
    account_runtime = get_account_runtime(request)
    session = await account_runtime.sessions.resolve(
        request.cookies.get(settings.SESSION_COOKIE_NAME),
    )
    overview = await account_runtime.paid_subscriptions.overview(
        user_id=session.user_id,
    )
    return SubscriptionPayload.from_overview(overview)


@router.post("/subscription/checkout", response_model=CheckoutPayload)
async def post_subscription_checkout(
    payload: CheckoutRequestPayload,
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> CheckoutPayload:
    settings = get_settings()
    _require_frontend_origin(request, settings)
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    account_runtime = get_account_runtime(request)
    session = await account_runtime.sessions.resolve(token)
    account_runtime.sessions.require_csrf(
        session_token=token or "",
        csrf_token=x_csrf_token,
    )
    checkout = await account_runtime.paid_subscriptions.checkout(
        user_id=session.user_id,
        plan_code=payload.plan_code,
    )
    return CheckoutPayload.from_checkout(checkout)


@router.post("/subscription/cancel", response_model=SubscriptionPayload)
async def post_subscription_cancel(
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> SubscriptionPayload:
    settings = get_settings()
    _require_frontend_origin(request, settings)
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    account_runtime = get_account_runtime(request)
    session = await account_runtime.sessions.resolve(token)
    account_runtime.sessions.require_csrf(
        session_token=token or "",
        csrf_token=x_csrf_token,
    )
    overview = await account_runtime.paid_subscriptions.cancel(
        user_id=session.user_id,
    )
    return SubscriptionPayload.from_overview(overview)


def _require_frontend_origin(request: Request, settings: ApiSettings) -> None:
    origin = request.headers.get("origin")
    if origin is None or origin not in settings.FRONTEND_ORIGINS:
        raise InvalidCsrfError
