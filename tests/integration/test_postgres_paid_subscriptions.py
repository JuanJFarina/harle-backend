import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import asyncpg
import pytest

from harle_domain.accounts import (
    GoogleIdentity,
    GoogleRegistration,
    PaymentProvider,
    PaymentWebhook,
    ProviderAuthorizedPayment,
    ProviderSubscription,
    PublicPlan,
)
from harle_infrastructure.postgres import (
    PostgresPaymentSubscriptionRepository,
    PostgresWebAccountRepository,
    create_postgres_pool,
)
from harle_services.accounts import PaidSubscriptionService, first_monthly_period

DATABASE_URL = os.environ.get("TEST_POSTGRES_DATABASE_URL")
ROOT = Path(__file__).parents[2]
NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
NEXT_MONTH = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
SCHEMA_PATHS = (
    ROOT / "scripts" / "apply_multi_user_runtime.sql",
    ROOT / "scripts" / "apply_subscription_interactions.sql",
    ROOT / "scripts" / "apply_interaction_frequency.sql",
    ROOT / "scripts" / "apply_internal_expenses.sql",
    ROOT / "scripts" / "apply_internal_events.sql",
    ROOT / "scripts" / "apply_event_notification_quotas.sql",
    ROOT / "scripts" / "apply_telegram_dedup_ordering.sql",
    ROOT / "scripts" / "apply_bans_quotas.sql",
    ROOT / "scripts" / "apply_web_registration.sql",
    ROOT / "scripts" / "apply_paid_subscriptions.sql",
)


class FakeMercadoPago:
    def __init__(self) -> None:
        self.external_reference: UUID | None = None

    async def create_subscription(
        self,
        *,
        external_reference: UUID,
        plan: PublicPlan,
        payer_email: str,
        back_url: str,
    ) -> ProviderSubscription:
        assert plan.code == "basic"
        assert payer_email.endswith("@example.com")
        assert back_url == "https://app.test/suscripcion"
        self.external_reference = external_reference
        return self._subscription("pending")

    async def get_subscription(
        self,
        *,
        subscription_id: str,
    ) -> ProviderSubscription:
        assert subscription_id == "provider-subscription"
        return self._subscription("authorized")

    async def get_authorized_payment(
        self,
        *,
        invoice_id: str,
    ) -> ProviderAuthorizedPayment:
        assert invoice_id == "invoice-1"
        assert self.external_reference is not None
        return ProviderAuthorizedPayment(
            invoice_id=invoice_id,
            subscription_id="provider-subscription",
            external_reference=self.external_reference,
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
        assert subscription_id == "provider-subscription"
        return self._subscription("cancelled")

    def verify_webhook(self, webhook: PaymentWebhook) -> None:
        assert webhook.signature == "valid"

    def _subscription(self, status: str) -> ProviderSubscription:
        assert self.external_reference is not None
        return ProviderSubscription(
            id="provider-subscription",
            external_reference=self.external_reference,
            status=status,
            checkout_url="https://mercadopago.test/checkout",
            next_payment_at=NEXT_MONTH,
            created_at=NOW,
            updated_at=NOW,
        )


async def verify_paid_subscription_lifecycle(database_url: str) -> None:
    connection = await asyncpg.connect(database_url)
    try:
        for schema_path in SCHEMA_PATHS:
            await connection.execute(schema_path.read_text(encoding="utf-8"))
    finally:
        await connection.close()

    pool = await create_postgres_pool(
        database_url=database_url,
        min_size=1,
        max_size=3,
    )
    user_id: UUID | None = None
    request_id = f"payment-{uuid4()}"
    try:
        accounts = PostgresWebAccountRepository(pool)
        user_id = await accounts.find_or_create_google_user(
            registration=GoogleRegistration(
                identity=GoogleIdentity(
                    subject=f"google-{uuid4()}",
                    display_name="Paid User",
                    email=f"{uuid4()}@example.com",
                    email_verified=True,
                ),
                locale="es-AR",
                timezone="UTC",
                period=first_monthly_period(NOW),
                created_at=NOW,
            ),
        )
        repository = PostgresPaymentSubscriptionRepository(pool)
        provider = FakeMercadoPago()
        service = PaidSubscriptionService(
            repository=repository,
            accounts=accounts,
            provider=cast(PaymentProvider, provider),
            checkout_return_url="https://app.test/suscripcion",
            clock=lambda: NOW,
        )

        checkout = await service.checkout(user_id=user_id, plan_code="basic")
        assert checkout.url == "https://mercadopago.test/checkout"
        repeated = await service.checkout(user_id=user_id, plan_code="basic")
        assert repeated.subscription.id == checkout.subscription.id

        await service.process_webhook(
            PaymentWebhook(
                event_id=request_id,
                request_id=request_id,
                topic="subscription_authorized_payment",
                resource_id="invoice-1",
                signature="valid",
            ),
        )
        active = await service.overview(user_id=user_id)
        assert active.plan.code == "basic"
        assert active.status == "active"
        assert active.period.starts_at == NOW
        assert active.period.ends_at == NEXT_MONTH

        cancelled = await service.cancel(user_id=user_id)
        assert cancelled.cancel_at_period_end
        assert cancelled.status == "cancelled"

        expired_service = PaidSubscriptionService(
            repository=repository,
            accounts=accounts,
            provider=cast(PaymentProvider, provider),
            checkout_return_url="https://app.test/suscripcion",
            clock=lambda: NEXT_MONTH + timedelta(seconds=1),
        )
        assert await expired_service.expire_user(user_id=user_id)
        free = await expired_service.overview(user_id=user_id)
        assert free.plan.code == "free"
        assert free.status == "active"
    finally:
        await pool.close()
        cleanup = await asyncpg.connect(database_url)
        try:
            await cleanup.execute(
                "DELETE FROM payment_webhook_claims WHERE provider_event_id = $1",
                request_id,
            )
            if user_id is not None:
                await cleanup.execute("DELETE FROM users WHERE id = $1", user_id)
        finally:
            await cleanup.close()


@pytest.mark.skipif(
    DATABASE_URL is None,
    reason="TEST_POSTGRES_DATABASE_URL is not configured",
)
def test_paid_subscription_lifecycle() -> None:
    assert DATABASE_URL is not None
    asyncio.run(verify_paid_subscription_lifecycle(DATABASE_URL))
