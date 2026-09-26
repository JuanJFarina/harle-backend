import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from hmac import new
from typing import cast
from uuid import UUID, uuid4

import pytest

from harle_domain.accounts import (
    AccountOverview,
    PaymentProvider,
    PaymentSubscription,
    PaymentSubscriptionRepository,
    PaymentSubscriptionStatus,
    PaymentWebhook,
    ProviderAuthorizedPayment,
    ProviderSubscription,
    PublicPlan,
    SubscriptionPeriod,
    SubscriptionStatus,
    WebAccountRepository,
)
from harle_infrastructure.mercado_pago import MercadoPagoClient
from harle_services.accounts import PaidSubscriptionService
from harle_utils import InvalidPaymentWebhookError

NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
NEXT_MONTH = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
USER_ID = uuid4()
PLAN = PublicPlan(
    code="basic",
    display_name="Básico",
    monthly_price=Decimal("5000"),
    currency="ARS",
    billing_interval="month",
    conversation_limit=480,
    notification_limit=60,
)


class FakeAccounts:
    async def get_overview(self, *, user_id: UUID) -> AccountOverview:
        return AccountOverview(
            user_id=user_id,
            display_name="User",
            plan_code="free",
            subscription_status=SubscriptionStatus.ACTIVE,
            subscription_period=SubscriptionPeriod(NOW, NEXT_MONTH),
            telegram_linked=True,
        )


class FakePayments:
    def __init__(self) -> None:
        self.subscription: PaymentSubscription | None = None
        self.completed = False
        self.reconciled_period: SubscriptionPeriod | None = None

    async def list_public_plans(self) -> list[PublicPlan]:
        return [PLAN]

    async def get_public_plan(self, *, code: str) -> PublicPlan | None:
        return PLAN if code == PLAN.code else None

    async def get_verified_email(self, *, user_id: UUID) -> str:
        assert user_id == USER_ID
        return "user@example.com"

    async def get_open_for_user(
        self,
        *,
        user_id: UUID,
    ) -> PaymentSubscription | None:
        assert user_id == USER_ID
        return self.subscription

    async def begin_checkout(
        self,
        *,
        user_id: UUID,
        plan_code: str,
        payer_email: str,
        created_at: datetime,
    ) -> PaymentSubscription:
        if self.subscription is None:
            self.subscription = _subscription(
                user_id=user_id,
                plan_code=plan_code,
                payer_email=payer_email,
                created_at=created_at,
            )
        return self.subscription

    async def attach_provider_subscription(
        self,
        *,
        subscription_id: UUID,
        provider: ProviderSubscription,
        updated_at: datetime,
    ) -> PaymentSubscription:
        assert self.subscription is not None
        assert self.subscription.id == subscription_id
        self.subscription = replace(
            self.subscription,
            status=PaymentSubscriptionStatus.PENDING,
            provider_status=provider.status,
            provider_subscription_id=provider.id,
            checkout_url=provider.checkout_url,
            next_payment_at=provider.next_payment_at,
            updated_at=updated_at,
        )
        return self.subscription

    async def claim_webhook(
        self,
        *,
        webhook: PaymentWebhook,
        received_at: datetime,
    ) -> bool:
        del webhook, received_at
        return True

    async def complete_webhook(
        self,
        *,
        event_id: str,
        processed_at: datetime,
    ) -> None:
        del event_id, processed_at
        self.completed = True

    async def fail_webhook(
        self,
        *,
        event_id: str,
        failed_at: datetime,
    ) -> None:
        del event_id, failed_at

    async def reconcile_provider_subscription(
        self,
        *,
        provider: ProviderSubscription,
        updated_at: datetime,
    ) -> PaymentSubscription:
        assert self.subscription is not None
        self.subscription = replace(
            self.subscription,
            provider_status=provider.status,
            next_payment_at=provider.next_payment_at,
            updated_at=updated_at,
        )
        return self.subscription

    async def reconcile_authorized_payment(
        self,
        *,
        payment: ProviderAuthorizedPayment,
        period: SubscriptionPeriod | None,
        updated_at: datetime,
    ) -> PaymentSubscription:
        del payment, updated_at
        assert self.subscription is not None
        self.reconciled_period = period
        return self.subscription


class FakeProvider:
    def __init__(self, payments: FakePayments) -> None:
        self.payments = payments
        self.created = 0

    async def create_subscription(
        self,
        *,
        external_reference: UUID,
        plan: PublicPlan,
        payer_email: str,
        back_url: str,
    ) -> ProviderSubscription:
        del plan, payer_email, back_url
        self.created += 1
        return _provider_subscription(external_reference)

    async def get_subscription(
        self,
        *,
        subscription_id: str,
    ) -> ProviderSubscription:
        assert subscription_id == "provider-subscription"
        return _provider_subscription(
            cast(PaymentSubscription, self.payments.subscription).id,
            status="authorized",
        )

    async def get_authorized_payment(
        self,
        *,
        invoice_id: str,
    ) -> ProviderAuthorizedPayment:
        assert invoice_id == "invoice-1"
        return ProviderAuthorizedPayment(
            invoice_id=invoice_id,
            subscription_id="provider-subscription",
            external_reference=cast(
                PaymentSubscription,
                self.payments.subscription,
            ).id,
            payment_id="payment-1",
            payment_status="approved",
            amount=Decimal("5000"),
            currency="ARS",
            debit_at=NOW,
            updated_at=NOW,
        )

    async def cancel_subscription(
        self,
        *,
        subscription_id: str,
    ) -> ProviderSubscription:
        return await self.get_subscription(subscription_id=subscription_id)

    def verify_webhook(self, webhook: PaymentWebhook) -> None:
        del webhook


def test_checkout_is_idempotent_for_one_open_subscription() -> None:
    async def verify() -> None:
        payments = FakePayments()
        provider = FakeProvider(payments)
        service = PaidSubscriptionService(
            repository=cast(PaymentSubscriptionRepository, payments),
            accounts=cast(WebAccountRepository, FakeAccounts()),
            provider=cast(PaymentProvider, provider),
            checkout_return_url="https://app.test/suscripcion",
            clock=lambda: NOW,
        )

        first = await service.checkout(user_id=USER_ID, plan_code="basic")
        second = await service.checkout(user_id=USER_ID, plan_code="basic")

        assert first.url == second.url == "https://mercadopago.test/checkout"
        assert provider.created == 1

        test_payments = FakePayments()
        test_provider = FakeProvider(test_payments)
        test_service = PaidSubscriptionService(
            repository=cast(PaymentSubscriptionRepository, test_payments),
            accounts=cast(WebAccountRepository, FakeAccounts()),
            provider=cast(PaymentProvider, test_provider),
            checkout_return_url="https://app.test/suscripcion",
            testing=True,
            test_payer_email="buyer@testuser.com",
            clock=lambda: NOW,
        )
        test_checkout = await test_service.checkout(
            user_id=USER_ID,
            plan_code="basic",
        )
        assert test_checkout.subscription.payer_email == "buyer@testuser.com"

    asyncio.run(verify())


def test_approved_webhook_builds_the_paid_period() -> None:
    async def verify() -> None:
        repository = FakePayments()
        repository.subscription = replace(
            _subscription(
                user_id=USER_ID,
                plan_code="basic",
                payer_email="user@example.com",
                created_at=NOW,
            ),
            provider_subscription_id="provider-subscription",
            status=PaymentSubscriptionStatus.PENDING,
        )
        service = PaidSubscriptionService(
            repository=cast(PaymentSubscriptionRepository, repository),
            accounts=cast(WebAccountRepository, FakeAccounts()),
            provider=cast(PaymentProvider, FakeProvider(repository)),
            checkout_return_url="https://app.test/suscripcion",
            clock=lambda: NOW,
        )

        processed = await service.process_webhook(
            PaymentWebhook(
                event_id="event-1",
                request_id="request-1",
                topic="subscription_authorized_payment",
                resource_id="invoice-1",
                signature="signature",
            ),
        )

        assert processed
        assert repository.completed
        assert repository.reconciled_period == SubscriptionPeriod(NOW, NEXT_MONTH)

    asyncio.run(verify())


def test_mercado_pago_signature_is_verified() -> None:
    secret = "webhook-secret"
    manifest = "id:invoice-1;request-id:request-1;ts:123;"
    signature = new(secret.encode(), manifest.encode(), sha256).hexdigest()
    client = MercadoPagoClient("access-token", secret)
    webhook = PaymentWebhook(
        event_id="event-1",
        request_id="request-1",
        topic="subscription_authorized_payment",
        resource_id="INVOICE-1",
        signature=f"ts=123,v1={signature}",
    )

    client.verify_webhook(webhook)
    with pytest.raises(InvalidPaymentWebhookError):
        client.verify_webhook(replace(webhook, signature="ts=123,v1=invalid"))


def _subscription(
    *,
    user_id: UUID,
    plan_code: str,
    payer_email: str,
    created_at: datetime,
) -> PaymentSubscription:
    return PaymentSubscription(
        id=uuid4(),
        user_id=user_id,
        plan_code=plan_code,
        payer_email=payer_email,
        status=PaymentSubscriptionStatus.CREATING,
        provider_status="creating",
        provider_subscription_id=None,
        provider_updated_at=None,
        checkout_url=None,
        period=None,
        cancel_at_period_end=False,
        next_payment_at=None,
        latest_payment_at=None,
        latest_payment_updated_at=None,
        created_at=created_at,
        updated_at=created_at,
    )


def _provider_subscription(
    external_reference: UUID,
    *,
    status: str = "pending",
) -> ProviderSubscription:
    return ProviderSubscription(
        id="provider-subscription",
        external_reference=external_reference,
        status=status,
        checkout_url="https://mercadopago.test/checkout",
        next_payment_at=NEXT_MONTH,
        created_at=NOW,
        updated_at=NOW,
    )
