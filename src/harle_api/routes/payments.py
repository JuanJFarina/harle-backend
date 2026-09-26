from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from harle_api.dependencies import get_account_runtime
from harle_domain.accounts import PaymentWebhook
from harle_utils import InvalidPaymentWebhookError

router = APIRouter(prefix="/api/payments/mercado-pago")


@router.post("/webhook")
async def post_mercado_pago_webhook(
    payload: Mapping[str, object],
    request: Request,
    x_signature: Annotated[str | None, Header(alias="X-Signature")] = None,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> JSONResponse:
    resource_id = request.query_params.get("data.id")
    topic = _topic(payload)
    event_id = _event_id(payload)
    if (
        not resource_id
        or not topic
        or not event_id
        or not x_signature
        or not x_request_id
    ):
        raise InvalidPaymentWebhookError
    processed = await get_account_runtime(
        request,
    ).paid_subscriptions.process_webhook(
        PaymentWebhook(
            event_id=f"{topic}:{event_id}",
            request_id=x_request_id,
            topic=topic,
            resource_id=resource_id,
            signature=x_signature,
        ),
    )
    return JSONResponse({"ok": True, "processed": processed})


def _topic(payload: Mapping[str, object]) -> str | None:
    value = payload.get("type")
    if not isinstance(value, str):
        return None
    allowed = {
        "payment",
        "subscription_preapproval",
        "subscription_authorized_payment",
    }
    return value if value in allowed else None


def _event_id(payload: Mapping[str, object]) -> str | None:
    value = payload.get("id")
    if isinstance(value, bool) or value is None:
        return None
    return str(value)
