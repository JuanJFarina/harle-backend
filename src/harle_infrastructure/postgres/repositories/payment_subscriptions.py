from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TypeVar
from uuid import UUID, uuid4

import asyncpg

from harle_domain.accounts import (
    PaymentSubscription,
    PaymentSubscriptionStatus,
    PaymentWebhook,
    ProviderAuthorizedPayment,
    ProviderSubscription,
    PublicPlan,
    SubscriptionPeriod,
)
from harle_utils import SubscriptionConflictError

FieldT = TypeVar("FieldT")
OPEN_STATUSES = ("creating", "pending", "active", "past_due", "cancelled")


@dataclass(frozen=True, slots=True)
class PostgresPaymentSubscriptionRepository:
    pool: asyncpg.Pool

    async def list_public_plans(self) -> list[PublicPlan]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    code,
                    display_name,
                    monthly_price_ars,
                    currency,
                    billing_interval,
                    monthly_request_limit,
                    monthly_notification_limit
                FROM plans
                WHERE active
                ORDER BY monthly_price_ars, code
                """,
            )
        return [_public_plan(row) for row in rows]

    async def get_public_plan(self, *, code: str) -> PublicPlan | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    code,
                    display_name,
                    monthly_price_ars,
                    currency,
                    billing_interval,
                    monthly_request_limit,
                    monthly_notification_limit
                FROM plans
                WHERE code = $1
                    AND active
                """,
                code,
            )
        return _public_plan(row) if row is not None else None

    async def get_verified_email(self, *, user_id: UUID) -> str | None:
        async with self.pool.acquire() as connection:
            value = await connection.fetchval(
                """
                SELECT email
                FROM external_identities
                WHERE user_id = $1
                    AND provider = 'google'
                """,
                user_id,
            )
        return value if isinstance(value, str) else None

    async def begin_checkout(
        self,
        *,
        user_id: UUID,
        plan_code: str,
        payer_email: str,
        created_at: datetime,
    ) -> PaymentSubscription:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                owner = await connection.fetchval(
                    "SELECT id FROM users WHERE id = $1 FOR UPDATE",
                    user_id,
                )
                if owner != user_id:
                    raise RuntimeError("Subscription owner does not exist.")
                existing = await connection.fetchrow(
                    """
                    SELECT *
                    FROM payment_subscriptions
                    WHERE user_id = $1
                        AND status = ANY($2::text[])
                    FOR UPDATE
                    """,
                    user_id,
                    list(OPEN_STATUSES),
                )
                if existing is not None:
                    subscription = _payment_subscription(existing)
                    if subscription.plan_code != plan_code:
                        raise SubscriptionConflictError
                    return subscription
                row = await connection.fetchrow(
                    """
                    INSERT INTO payment_subscriptions (
                        id,
                        user_id,
                        plan_code,
                        payer_email,
                        status,
                        provider_status,
                        created_at,
                        updated_at
                    )
                    VALUES ($1, $2, $3, $4, 'creating', 'creating', $5, $5)
                    RETURNING *
                    """,
                    uuid4(),
                    user_id,
                    plan_code,
                    payer_email,
                    created_at,
                )
        if row is None:
            raise RuntimeError("Could not create the subscription checkout.")
        return _payment_subscription(row)

    async def attach_provider_subscription(
        self,
        *,
        subscription_id: UUID,
        provider: ProviderSubscription,
        updated_at: datetime,
    ) -> PaymentSubscription:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE payment_subscriptions
                SET provider_subscription_id = $2,
                    provider_status = $3,
                    checkout_url = $4,
                    next_payment_at = $5,
                    provider_updated_at = $6,
                    status = CASE
                        WHEN $3 = 'authorized' THEN status
                        ELSE 'pending'
                    END,
                    updated_at = $7
                WHERE id = $1
                    AND status IN ('creating', 'pending')
                RETURNING *
                """,
                subscription_id,
                provider.id,
                provider.status,
                provider.checkout_url,
                provider.next_payment_at,
                provider.updated_at,
                updated_at,
            )
        if row is None:
            raise RuntimeError("Could not attach the provider subscription.")
        return _payment_subscription(row)

    async def fail_checkout(
        self,
        *,
        subscription_id: UUID,
        updated_at: datetime,
    ) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE payment_subscriptions
                SET status = 'failed',
                    provider_status = 'rejected',
                    checkout_url = NULL,
                    updated_at = $2
                WHERE id = $1
                    AND status = 'creating'
                """,
                subscription_id,
                updated_at,
            )

    async def get_open_for_user(
        self,
        *,
        user_id: UUID,
    ) -> PaymentSubscription | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT *
                FROM payment_subscriptions
                WHERE user_id = $1
                    AND status = ANY($2::text[])
                ORDER BY created_at DESC
                LIMIT 1
                """,
                user_id,
                list(OPEN_STATUSES),
            )
        return _payment_subscription(row) if row is not None else None

    async def get_by_provider_id(
        self,
        *,
        provider_subscription_id: str,
    ) -> PaymentSubscription | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT *
                FROM payment_subscriptions
                WHERE provider_subscription_id = $1
                """,
                provider_subscription_id,
            )
        return _payment_subscription(row) if row is not None else None

    async def reconcile_provider_subscription(
        self,
        *,
        provider: ProviderSubscription,
        updated_at: datetime,
    ) -> PaymentSubscription | None:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT *
                    FROM payment_subscriptions
                    WHERE id = $1
                        AND (
                            provider_subscription_id IS NULL
                            OR provider_subscription_id = $2
                        )
                    FOR UPDATE
                    """,
                    provider.external_reference,
                    provider.id,
                )
                if row is None:
                    return None
                subscription = _payment_subscription(row)
                if (
                    subscription.provider_updated_at is not None
                    and subscription.provider_updated_at >= provider.updated_at
                ):
                    return subscription
                local_status = subscription.status.value
                if provider.status == "cancelled":
                    local_status = (
                        "ended" if subscription.period is None else "cancelled"
                    )
                elif provider.status == "paused":
                    local_status = "past_due"
                    if subscription.period is not None:
                        await _set_user_past_due(
                            connection,
                            user_id=subscription.user_id,
                            updated_at=updated_at,
                        )
                elif provider.status == "pending":
                    local_status = "pending"
                updated = await connection.fetchrow(
                    """
                    UPDATE payment_subscriptions
                    SET provider_subscription_id = $2,
                        provider_status = $3,
                        checkout_url = COALESCE($4, checkout_url),
                        next_payment_at = $5,
                        provider_updated_at = $6,
                        status = $7,
                        cancel_at_period_end = CASE
                            WHEN $3 = 'cancelled' THEN TRUE
                            ELSE cancel_at_period_end
                        END,
                        updated_at = $8
                    WHERE id = $1
                    RETURNING *
                    """,
                    subscription.id,
                    provider.id,
                    provider.status,
                    provider.checkout_url,
                    provider.next_payment_at,
                    provider.updated_at,
                    local_status,
                    updated_at,
                )
        return _payment_subscription(updated) if updated is not None else None

    async def reconcile_authorized_payment(
        self,
        *,
        payment: ProviderAuthorizedPayment,
        period: SubscriptionPeriod | None,
        updated_at: datetime,
    ) -> PaymentSubscription | None:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT *
                    FROM payment_subscriptions
                    WHERE provider_subscription_id = $1
                    FOR UPDATE
                    """,
                    payment.subscription_id,
                )
                if row is None:
                    return None
                subscription = _payment_subscription(row)
                if payment.external_reference != subscription.id:
                    raise ValueError(
                        "Payment external reference does not match subscription.",
                    )
                if (
                    subscription.latest_payment_updated_at is not None
                    and subscription.latest_payment_updated_at >= payment.updated_at
                ):
                    return subscription
                owner_status = await connection.fetchval(
                    """
                    SELECT subscription_status
                    FROM users
                    WHERE id = $1
                    FOR UPDATE
                    """,
                    subscription.user_id,
                )
                await connection.execute(
                    """
                    INSERT INTO subscription_payments (
                        subscription_id,
                        provider_invoice_id,
                        provider_payment_id,
                        status,
                        amount,
                        currency,
                        debit_at,
                        created_at,
                        updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8)
                    ON CONFLICT (provider_invoice_id) DO UPDATE
                    SET provider_payment_id = EXCLUDED.provider_payment_id,
                        status = EXCLUDED.status,
                        amount = EXCLUDED.amount,
                        currency = EXCLUDED.currency,
                        debit_at = EXCLUDED.debit_at,
                        updated_at = EXCLUDED.updated_at
                    """,
                    subscription.id,
                    payment.invoice_id,
                    payment.payment_id,
                    _local_payment_status(payment.payment_status),
                    payment.amount,
                    payment.currency,
                    payment.debit_at,
                    updated_at,
                )
                if owner_status not in {"active", "past_due"}:
                    return subscription
                if payment.payment_status == "approved":
                    if period is None:
                        raise ValueError("Approved payment requires a period.")
                    local_status = (
                        "cancelled" if subscription.cancel_at_period_end else "active"
                    )
                    await connection.execute(
                        """
                        UPDATE users
                        SET plan_code = $2,
                            subscription_status = 'active',
                            subscription_valid_until = NULL,
                            subscription_period_starts_at = $3,
                            subscription_period_ends_at = $4,
                            subscription_synced_at = $5,
                            updated_at = $5
                        WHERE id = $1
                        """,
                        subscription.user_id,
                        subscription.plan_code,
                        period.starts_at,
                        period.ends_at,
                        updated_at,
                    )
                elif payment.payment_status in {
                    "rejected",
                    "cancelled",
                    "refunded",
                    "charged_back",
                }:
                    if subscription.cancel_at_period_end:
                        return subscription
                    local_status = "past_due"
                    if subscription.period is not None:
                        await _set_user_past_due(
                            connection,
                            user_id=subscription.user_id,
                            updated_at=updated_at,
                        )
                else:
                    return subscription
                updated = await connection.fetchrow(
                    """
                    UPDATE payment_subscriptions
                    SET status = $2,
                        period_starts_at = COALESCE($3, period_starts_at),
                        period_ends_at = COALESCE($4, period_ends_at),
                        latest_payment_at = $5,
                        latest_payment_updated_at = $6,
                        updated_at = $7
                    WHERE id = $1
                    RETURNING *
                    """,
                    subscription.id,
                    local_status,
                    period.starts_at if period is not None else None,
                    period.ends_at if period is not None else None,
                    payment.debit_at,
                    payment.updated_at,
                    updated_at,
                )
        return _payment_subscription(updated) if updated is not None else None

    async def cancel_at_period_end(
        self,
        *,
        subscription_id: UUID,
        provider_status: str,
        updated_at: datetime,
    ) -> PaymentSubscription:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE payment_subscriptions
                SET status = CASE
                        WHEN period_ends_at IS NULL THEN 'ended'
                        ELSE 'cancelled'
                    END,
                    provider_status = $2,
                    cancel_at_period_end = TRUE,
                    checkout_url = NULL,
                    updated_at = $3
                WHERE id = $1
                RETURNING *
                """,
                subscription_id,
                provider_status,
                updated_at,
            )
        if row is None:
            raise RuntimeError("Could not cancel the subscription.")
        return _payment_subscription(row)

    async def expire_user_paid_subscription(
        self,
        *,
        user_id: UUID,
        current_time: datetime,
        free_period: SubscriptionPeriod,
    ) -> bool:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT id
                    FROM payment_subscriptions
                    WHERE user_id = $1
                        AND status = 'cancelled'
                        AND period_ends_at <= $2
                    FOR UPDATE
                    """,
                    user_id,
                    current_time,
                )
                if row is None:
                    return False
                await _expire_subscription(
                    connection,
                    subscription_id=_required(row, "id", UUID),
                    user_id=user_id,
                    current_time=current_time,
                    free_period=free_period,
                )
        return True

    async def expire_due_paid_subscriptions(
        self,
        *,
        current_time: datetime,
        free_period: SubscriptionPeriod,
        limit: int,
    ) -> int:
        async with self.pool.acquire() as connection:
            user_ids = await connection.fetch(
                """
                SELECT user_id
                FROM payment_subscriptions
                WHERE status = 'cancelled'
                    AND period_ends_at <= $1
                ORDER BY period_ends_at, id
                LIMIT $2
                """,
                current_time,
                limit,
            )
        changed = 0
        for row in user_ids:
            changed += int(
                await self.expire_user_paid_subscription(
                    user_id=_required(row, "user_id", UUID),
                    current_time=current_time,
                    free_period=free_period,
                ),
            )
        return changed

    async def claim_webhook(
        self,
        *,
        webhook: PaymentWebhook,
        received_at: datetime,
    ) -> bool:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO payment_webhook_claims (
                    provider_event_id,
                    provider_request_id,
                    topic,
                    resource_id,
                    status,
                    received_at
                )
                VALUES ($1, $2, $3, $4, 'processing', $5)
                ON CONFLICT (provider_event_id) DO UPDATE
                SET status = 'processing',
                    provider_request_id = EXCLUDED.provider_request_id,
                    received_at = EXCLUDED.received_at
                WHERE payment_webhook_claims.status IN ('failed', 'processing')
                RETURNING provider_event_id
                """,
                webhook.event_id,
                webhook.request_id,
                webhook.topic,
                webhook.resource_id,
                received_at,
            )
        return row is not None

    async def complete_webhook(
        self,
        *,
        event_id: str,
        processed_at: datetime,
    ) -> None:
        await self._set_webhook_state(
            event_id=event_id,
            status="processed",
            processed_at=processed_at,
        )

    async def fail_webhook(
        self,
        *,
        event_id: str,
        failed_at: datetime,
    ) -> None:
        await self._set_webhook_state(
            event_id=event_id,
            status="failed",
            processed_at=failed_at,
        )

    async def _set_webhook_state(
        self,
        *,
        event_id: str,
        status: str,
        processed_at: datetime,
    ) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE payment_webhook_claims
                SET status = $2,
                    processed_at = $3
                WHERE provider_event_id = $1
                """,
                event_id,
                status,
                processed_at,
            )


async def _set_user_past_due(
    connection: asyncpg.Connection,
    *,
    user_id: UUID,
    updated_at: datetime,
) -> None:
    await connection.execute(
        """
        UPDATE users
        SET subscription_status = 'past_due',
            subscription_synced_at = $2,
            updated_at = $2
        WHERE id = $1
            AND subscription_status IN ('active', 'past_due')
        """,
        user_id,
        updated_at,
    )


async def _expire_subscription(
    connection: asyncpg.Connection,
    *,
    subscription_id: UUID,
    user_id: UUID,
    current_time: datetime,
    free_period: SubscriptionPeriod,
) -> None:
    await connection.execute(
        """
        UPDATE payment_subscriptions
        SET status = 'ended',
            updated_at = $2
        WHERE id = $1
        """,
        subscription_id,
        current_time,
    )
    await connection.execute(
        """
        UPDATE users
        SET plan_code = 'free',
            subscription_status = 'active',
            subscription_valid_until = NULL,
            subscription_period_starts_at = $2,
            subscription_period_ends_at = $3,
            subscription_synced_at = $4,
            updated_at = $4
        WHERE id = $1
            AND subscription_status <> 'revoked'
        """,
        user_id,
        free_period.starts_at,
        free_period.ends_at,
        current_time,
    )


def _public_plan(row: asyncpg.Record) -> PublicPlan:
    return PublicPlan(
        code=_required(row, "code", str),
        display_name=_required(row, "display_name", str),
        monthly_price=_required(row, "monthly_price_ars", Decimal),
        currency=_required(row, "currency", str),
        billing_interval=_required(row, "billing_interval", str),
        conversation_limit=_required(row, "monthly_request_limit", int),
        notification_limit=_required(row, "monthly_notification_limit", int),
    )


def _payment_subscription(row: asyncpg.Record) -> PaymentSubscription:
    starts_at = _optional(row, "period_starts_at", datetime)
    ends_at = _optional(row, "period_ends_at", datetime)
    period = (
        SubscriptionPeriod(starts_at, ends_at)
        if starts_at is not None and ends_at is not None
        else None
    )
    return PaymentSubscription(
        id=_required(row, "id", UUID),
        user_id=_required(row, "user_id", UUID),
        plan_code=_required(row, "plan_code", str),
        payer_email=_required(row, "payer_email", str),
        status=PaymentSubscriptionStatus(_required(row, "status", str)),
        provider_status=_required(row, "provider_status", str),
        provider_subscription_id=_optional(row, "provider_subscription_id", str),
        provider_updated_at=_optional(row, "provider_updated_at", datetime),
        checkout_url=_optional(row, "checkout_url", str),
        period=period,
        cancel_at_period_end=_required(row, "cancel_at_period_end", bool),
        next_payment_at=_optional(row, "next_payment_at", datetime),
        latest_payment_at=_optional(row, "latest_payment_at", datetime),
        latest_payment_updated_at=_optional(
            row,
            "latest_payment_updated_at",
            datetime,
        ),
        created_at=_required(row, "created_at", datetime),
        updated_at=_required(row, "updated_at", datetime),
    )


def _local_payment_status(provider_status: str) -> str:
    aliases = {"canceled": "cancelled"}
    normalized = aliases.get(provider_status, provider_status)
    allowed = {
        "pending",
        "approved",
        "rejected",
        "cancelled",
        "refunded",
        "charged_back",
    }
    return normalized if normalized in allowed else "pending"


def _required(
    row: asyncpg.Record,
    key: str,
    expected: type[FieldT],
) -> FieldT:
    value: object = row[key]
    if not isinstance(value, expected):
        raise TypeError(f"Unexpected {key} value.")
    return value


def _optional(
    row: asyncpg.Record,
    key: str,
    expected: type[FieldT],
) -> FieldT | None:
    value: object = row[key]
    if value is None:
        return None
    if not isinstance(value, expected):
        raise TypeError(f"Unexpected {key} value.")
    return value
