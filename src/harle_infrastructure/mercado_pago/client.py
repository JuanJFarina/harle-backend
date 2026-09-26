from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from hmac import compare_digest, new
from typing import cast
from uuid import UUID

import httpx

from harle_domain.accounts import (
    PaymentWebhook,
    ProviderAuthorizedPayment,
    ProviderSubscription,
    PublicPlan,
)
from harle_utils import (
    InvalidPaymentWebhookError,
    PaymentProviderError,
    PaymentProviderRejectedError,
)


@dataclass(frozen=True, slots=True)
class MercadoPagoClient:
    access_token: str
    webhook_secret: str
    api_url: str = "https://api.mercadopago.com"
    timeout_seconds: float = 15

    async def create_subscription(
        self,
        *,
        external_reference: UUID,
        plan: PublicPlan,
        payer_email: str,
        back_url: str,
    ) -> ProviderSubscription:
        payload: Mapping[str, object] = {
            "reason": f"Harle - Plan {plan.display_name}",
            "external_reference": str(external_reference),
            "payer_email": payer_email,
            "auto_recurring": {
                "frequency": 1,
                "frequency_type": "months",
                "transaction_amount": float(plan.monthly_price),
                "currency_id": plan.currency,
            },
            "back_url": back_url,
            "status": "pending",
        }
        response = await self._request(
            "POST",
            "/preapproval",
            payload=payload,
            idempotency_key=str(external_reference),
        )
        return _provider_subscription(response)

    async def get_subscription(
        self,
        *,
        subscription_id: str,
    ) -> ProviderSubscription:
        response = await self._request(
            "GET",
            f"/preapproval/{subscription_id}",
        )
        return _provider_subscription(response)

    async def get_authorized_payment(
        self,
        *,
        invoice_id: str,
    ) -> ProviderAuthorizedPayment:
        response = await self._request(
            "GET",
            f"/authorized_payments/{invoice_id}",
        )
        return _provider_authorized_payment(response)

    async def cancel_subscription(
        self,
        *,
        subscription_id: str,
    ) -> ProviderSubscription:
        response = await self._request(
            "PUT",
            f"/preapproval/{subscription_id}",
            payload={"status": "canceled"},
            idempotency_key=f"cancel:{subscription_id}",
        )
        return _provider_subscription(response)

    def verify_webhook(self, webhook: PaymentWebhook) -> None:
        parts = {}
        for item in webhook.signature.split(","):
            key, separator, value = item.strip().partition("=")
            if separator:
                parts[key] = value
        timestamp = parts.get("ts")
        signature = parts.get("v1")
        if not timestamp or not signature:
            raise InvalidPaymentWebhookError
        manifest = (
            f"id:{webhook.resource_id.lower()};"
            f"request-id:{webhook.request_id};"
            f"ts:{timestamp};"
        )
        expected = new(
            self.webhook_secret.encode(),
            manifest.encode(),
            sha256,
        ).hexdigest()
        if not compare_digest(signature, expected):
            raise InvalidPaymentWebhookError

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, object] | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, object]:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        if idempotency_key is not None:
            headers["X-Idempotency-Key"] = idempotency_key
        try:
            async with httpx.AsyncClient(
                base_url=self.api_url,
                timeout=self.timeout_seconds,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    headers=headers,
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise PaymentProviderError from exc
        if 400 <= response.status_code < 500 and response.status_code != 429:
            raise PaymentProviderRejectedError(
                f"Mercado Pago rejected the request with status {response.status_code}.",
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise PaymentProviderError(
                f"Mercado Pago returned status {response.status_code}.",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise PaymentProviderRejectedError from exc
        if not isinstance(body, Mapping):
            raise PaymentProviderRejectedError(
                "Mercado Pago returned an invalid payload.",
            )
        return body


def _provider_subscription(payload: Mapping[str, object]) -> ProviderSubscription:
    return ProviderSubscription(
        id=_text(payload, "id"),
        external_reference=UUID(_text(payload, "external_reference")),
        status=_subscription_status(_text(payload, "status")),
        checkout_url=_optional_text(payload, "init_point"),
        next_payment_at=_optional_datetime(payload, "next_payment_date"),
        created_at=_datetime(payload, "date_created"),
        updated_at=_datetime(payload, "last_modified"),
    )


def _provider_authorized_payment(
    payload: Mapping[str, object],
) -> ProviderAuthorizedPayment:
    payment = payload.get("payment")
    typed_payment = (
        cast(Mapping[str, object], payment) if isinstance(payment, Mapping) else {}
    )
    raw_payment_id = typed_payment.get("id")
    payment_id = str(raw_payment_id) if raw_payment_id is not None else None
    raw_payment_status = typed_payment.get("status")
    payment_status = (
        _payment_status(raw_payment_status)
        if isinstance(raw_payment_status, str)
        else "pending"
    )
    return ProviderAuthorizedPayment(
        invoice_id=str(_value(payload, "id")),
        subscription_id=_text(payload, "preapproval_id"),
        external_reference=UUID(_text(payload, "external_reference")),
        payment_id=payment_id,
        payment_status=payment_status,
        amount=_decimal(payload, "transaction_amount"),
        currency=_text(payload, "currency_id"),
        debit_at=_datetime(payload, "debit_date"),
        updated_at=_datetime(payload, "last_modified"),
    )


def _subscription_status(value: str) -> str:
    return "cancelled" if value == "canceled" else value


def _payment_status(value: str) -> str:
    return "cancelled" if value == "canceled" else value


def _value(payload: Mapping[str, object], key: str) -> object:
    value = payload.get(key)
    if value is None:
        raise PaymentProviderRejectedError(f"Mercado Pago omitted {key}.")
    return value


def _text(payload: Mapping[str, object], key: str) -> str:
    value = _value(payload, key)
    if not isinstance(value, str) or not value.strip():
        raise PaymentProviderRejectedError(
            f"Mercado Pago returned invalid {key}.",
        )
    return value.strip()


def _optional_text(payload: Mapping[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PaymentProviderRejectedError(
            f"Mercado Pago returned invalid {key}.",
        )
    return value.strip()


def _datetime(payload: Mapping[str, object], key: str) -> datetime:
    value = _text(payload, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaymentProviderRejectedError(
            f"Mercado Pago returned invalid {key}.",
        ) from exc
    if parsed.tzinfo is None:
        raise PaymentProviderRejectedError(
            f"Mercado Pago returned naive {key}.",
        )
    return parsed


def _optional_datetime(
    payload: Mapping[str, object],
    key: str,
) -> datetime | None:
    if payload.get(key) is None:
        return None
    return _datetime(payload, key)


def _decimal(payload: Mapping[str, object], key: str) -> Decimal:
    value = _value(payload, key)
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise PaymentProviderRejectedError(
            f"Mercado Pago returned invalid {key}.",
        ) from exc
