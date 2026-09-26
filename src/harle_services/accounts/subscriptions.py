from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from harle_domain.accounts import (
    PaymentCheckout,
    PaymentProvider,
    PaymentSubscriptionRepository,
    PaymentSubscriptionStatus,
    PaymentWebhook,
    PublicPlan,
    SubscriptionOverview,
    SubscriptionPeriod,
    SubscriptionStatus,
    User,
    WebAccountRepository,
)
from harle_utils import (
    Clock,
    PaymentProviderError,
    PaymentProviderRejectedError,
    SubscriptionConflictError,
    as_utc,
    utc_now,
)

from .free_periods import FreeSubscriptionService, first_monthly_period


@dataclass(frozen=True, slots=True)
class PaidSubscriptionService:
    repository: PaymentSubscriptionRepository
    accounts: WebAccountRepository
    provider: PaymentProvider
    checkout_return_url: str
    clock: Clock = utc_now

    async def list_plans(self) -> list[PublicPlan]:
        return list(await self.repository.list_public_plans())

    async def checkout(
        self,
        *,
        user_id: UUID,
        plan_code: str,
    ) -> PaymentCheckout:
        plan = await self.repository.get_public_plan(code=plan_code)
        if plan is None or plan.code not in {"basic", "max"}:
            raise ValueError("The selected paid plan is unavailable.")
        account = await self.accounts.get_overview(user_id=user_id)
        if account is None:
            raise SubscriptionConflictError
        existing = await self.repository.get_open_for_user(user_id=user_id)
        if account.subscription_status is not SubscriptionStatus.ACTIVE:
            raise SubscriptionConflictError
        if account.plan_code != "free" and existing is None:
            raise SubscriptionConflictError
        email = await self.repository.get_verified_email(user_id=user_id)
        if email is None:
            raise SubscriptionConflictError
        current_time = self._now()
        subscription = await self.repository.begin_checkout(
            user_id=user_id,
            plan_code=plan.code,
            payer_email=email,
            created_at=current_time,
        )
        if subscription.checkout_url is not None:
            return PaymentCheckout(subscription, subscription.checkout_url)
        try:
            provider = await self.provider.create_subscription(
                external_reference=subscription.id,
                plan=plan,
                payer_email=email,
                back_url=self.checkout_return_url,
            )
        except PaymentProviderRejectedError:
            await self.repository.fail_checkout(
                subscription_id=subscription.id,
                updated_at=self._now(),
            )
            raise
        attached = await self.repository.attach_provider_subscription(
            subscription_id=subscription.id,
            provider=provider,
            updated_at=self._now(),
        )
        if attached.checkout_url is None:
            raise PaymentProviderError("Mercado Pago omitted the checkout URL.")
        return PaymentCheckout(attached, attached.checkout_url)

    async def overview(self, *, user_id: UUID) -> SubscriptionOverview:
        await self.expire_user(user_id=user_id)
        account = await self.accounts.get_overview(user_id=user_id)
        if account is None:
            raise SubscriptionConflictError
        subscription = await self.repository.get_open_for_user(user_id=user_id)
        plan = await self.repository.get_public_plan(code=account.plan_code)
        if plan is None:
            raise RuntimeError("The account plan is unavailable.")
        pending_plan = None
        if (
            subscription is not None
            and subscription.status
            in {
                PaymentSubscriptionStatus.CREATING,
                PaymentSubscriptionStatus.PENDING,
            }
        ):
            pending_plan = await self.repository.get_public_plan(
                code=subscription.plan_code,
            )
        if subscription is None:
            return SubscriptionOverview(
                plan=plan,
                pending_plan=None,
                status="manual" if plan.code != "free" else "active",
                period=account.subscription_period,
                provider_status=None,
                checkout_url=None,
                cancel_at_period_end=False,
                next_payment_at=None,
            )
        return SubscriptionOverview(
            plan=plan,
            pending_plan=pending_plan,
            status=subscription.status.value,
            period=account.subscription_period,
            provider_status=subscription.provider_status,
            checkout_url=subscription.checkout_url,
            cancel_at_period_end=subscription.cancel_at_period_end,
            next_payment_at=subscription.next_payment_at,
        )

    async def cancel(self, *, user_id: UUID) -> SubscriptionOverview:
        subscription = await self.repository.get_open_for_user(user_id=user_id)
        if (
            subscription is None
            or subscription.provider_subscription_id is None
            or subscription.status is PaymentSubscriptionStatus.CANCELLED
        ):
            raise SubscriptionConflictError
        provider = await self.provider.cancel_subscription(
            subscription_id=subscription.provider_subscription_id,
        )
        await self.repository.cancel_at_period_end(
            subscription_id=subscription.id,
            provider_status=provider.status,
            updated_at=self._now(),
        )
        return await self.overview(user_id=user_id)

    async def process_webhook(self, webhook: PaymentWebhook) -> bool:
        self.provider.verify_webhook(webhook)
        current_time = self._now()
        claimed = await self.repository.claim_webhook(
            webhook=webhook,
            received_at=current_time,
        )
        if not claimed:
            return False
        try:
            if webhook.topic == "subscription_preapproval":
                provider = await self.provider.get_subscription(
                    subscription_id=webhook.resource_id,
                )
                await self.repository.reconcile_provider_subscription(
                    provider=provider,
                    updated_at=self._now(),
                )
            elif webhook.topic == "subscription_authorized_payment":
                payment = await self.provider.get_authorized_payment(
                    invoice_id=webhook.resource_id,
                )
                provider = await self.provider.get_subscription(
                    subscription_id=payment.subscription_id,
                )
                await self.repository.reconcile_provider_subscription(
                    provider=provider,
                    updated_at=self._now(),
                )
                period = _payment_period(payment.debit_at, provider.next_payment_at)
                await self.repository.reconcile_authorized_payment(
                    payment=payment,
                    period=period if payment.payment_status == "approved" else None,
                    updated_at=self._now(),
                )
            await self.repository.complete_webhook(
                event_id=webhook.event_id,
                processed_at=self._now(),
            )
        except Exception:
            await self.repository.fail_webhook(
                event_id=webhook.event_id,
                failed_at=self._now(),
            )
            raise
        return True

    async def expire_user(self, *, user_id: UUID) -> bool:
        current_time = self._now()
        return await self.repository.expire_user_paid_subscription(
            user_id=user_id,
            current_time=current_time,
            free_period=first_monthly_period(current_time),
        )

    async def expire_due(self, *, limit: int = 500) -> int:
        current_time = self._now()
        return await self.repository.expire_due_paid_subscriptions(
            current_time=current_time,
            free_period=first_monthly_period(current_time),
            limit=limit,
        )

    def _now(self) -> datetime:
        return as_utc(self.clock())


@dataclass(frozen=True, slots=True)
class SubscriptionMaintenanceService:
    free: FreeSubscriptionService
    paid: PaidSubscriptionService

    async def ensure_current(self, user: User) -> bool:
        if await self.paid.expire_user(user_id=user.id):
            return True
        return await self.free.ensure_current(user)

    async def ensure_user(self, user_id: UUID) -> bool:
        if await self.paid.expire_user(user_id=user_id):
            return True
        return await self.free.ensure_user(user_id)

    async def renew_due(self, *, limit: int = 500) -> int:
        expired = await self.paid.expire_due(limit=limit)
        renewed = await self.free.renew_due(limit=limit)
        return expired + renewed


def _payment_period(
    starts_at: datetime,
    provider_ends_at: datetime | None,
) -> SubscriptionPeriod:
    if provider_ends_at is not None and provider_ends_at > starts_at:
        return SubscriptionPeriod(
            starts_at=as_utc(starts_at),
            ends_at=as_utc(provider_ends_at),
        )
    return first_monthly_period(as_utc(starts_at))
