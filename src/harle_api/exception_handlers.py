import re
from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from harle_utils import (
    AccessDeniedError,
    AuthenticationRequiredError,
    InvalidCsrfError,
    InvalidOAuthError,
    InvalidPaymentWebhookError,
    OAuthProviderError,
    PaymentProviderError,
    SubscriptionConflictError,
    TelegramAlreadyLinkedError,
    log,
)

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
REQUEST_ID_HEADER = "X-Request-ID"


async def access_denied_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del error
    if _is_browser_api(request):
        return _web_error(
            request,
            403,
            "access_denied",
            "Access is denied.",
        )
    return JSONResponse(content={"ok": True, "accepted": False})


async def authentication_required_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del error
    return _web_error(
        request,
        401,
        "authentication_required",
        "Authentication is required.",
    )


async def invalid_csrf_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del error
    return _web_error(
        request,
        403,
        "invalid_csrf",
        "The request could not be verified.",
    )


async def invalid_oauth_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del error
    return _web_error(
        request,
        400,
        "invalid_oauth",
        "Google authentication failed.",
    )


async def oauth_provider_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del error
    return _web_error(
        request,
        502,
        "oauth_provider_unavailable",
        "Google authentication is temporarily unavailable.",
    )


async def telegram_already_linked_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del error
    return _web_error(
        request,
        409,
        "telegram_link_conflict",
        "A Telegram account is already linked.",
    )


async def subscription_conflict_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del error
    return _web_error(
        request,
        409,
        "subscription_conflict",
        "The subscription cannot be changed in its current state.",
    )


async def invalid_payment_webhook_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del error
    return _web_error(
        request,
        401,
        "invalid_payment_webhook",
        "The payment notification could not be verified.",
    )


async def payment_provider_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del error
    return _web_error(
        request,
        502,
        "payment_provider_unavailable",
        "The payment provider is temporarily unavailable.",
    )


async def request_validation_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    if not isinstance(error, RequestValidationError):
        raise TypeError("Unexpected request validation error.")
    if not _is_browser_api(request):
        return JSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(error.errors())},
        )
    return _web_error(
        request,
        422,
        "validation_error",
        "The request is invalid.",
    )


async def unexpected_error_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    del error
    request_id = _request_id(request)
    scope = "browser_api" if _is_browser_api(request) else "telegram_or_system"
    log.error("Unexpected request failure request_id=%s scope=%s", request_id, scope)
    if _is_browser_api(request):
        return _web_error(
            request,
            500,
            "internal_error",
            "The request could not be completed.",
        )
    return JSONResponse(
        status_code=500,
        content={"ok": False, "accepted": False},
        headers={REQUEST_ID_HEADER: request_id},
    )


def _web_error(
    request: Request,
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    request_id = _request_id(request)
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "fields": [],
                "request_id": request_id,
            },
        },
        headers={REQUEST_ID_HEADER: request_id},
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AccessDeniedError, access_denied_handler)
    app.add_exception_handler(
        AuthenticationRequiredError,
        authentication_required_handler,
    )
    app.add_exception_handler(InvalidCsrfError, invalid_csrf_handler)
    app.add_exception_handler(InvalidOAuthError, invalid_oauth_handler)
    app.add_exception_handler(OAuthProviderError, oauth_provider_handler)
    app.add_exception_handler(
        TelegramAlreadyLinkedError,
        telegram_already_linked_handler,
    )
    app.add_exception_handler(
        SubscriptionConflictError,
        subscription_conflict_handler,
    )
    app.add_exception_handler(
        InvalidPaymentWebhookError,
        invalid_payment_webhook_handler,
    )
    app.add_exception_handler(PaymentProviderError, payment_provider_handler)
    app.add_exception_handler(RequestValidationError, request_validation_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)


def register_request_id_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def assign_request_id(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.request_id = _incoming_request_id(request)
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request.state.request_id
        return response


def _incoming_request_id(request: Request) -> str:
    candidate = request.headers.get(REQUEST_ID_HEADER)
    if candidate is not None and REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return uuid4().hex


def _request_id(request: Request) -> str:
    request_id = getattr(request.state, "request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id
    generated = _incoming_request_id(request)
    request.state.request_id = generated
    return generated


def _is_browser_api(request: Request) -> bool:
    return request.url.path == "/api" or request.url.path.startswith("/api/")
