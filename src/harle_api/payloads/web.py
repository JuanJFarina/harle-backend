from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from harle_domain.accounts import (
    PaymentCheckout,
    PublicPlan,
    SubscriptionOverview,
)
from harle_services.accounts import (
    TelegramLinkView,
    WebSessionResult,
)


class TelegramLinkStatusPayload(BaseModel):
    state: str
    expires_at: datetime | None = None


class SessionPayload(BaseModel):
    user_id: UUID
    display_name: str
    plan_code: str
    subscription_period_starts_at: datetime
    subscription_period_ends_at: datetime
    telegram_link: TelegramLinkStatusPayload
    csrf_token: str
    session_expires_at: datetime

    @classmethod
    def from_view(cls, view: WebSessionResult) -> "SessionPayload":
        account = view.account
        return cls(
            user_id=account.user_id,
            display_name=account.display_name,
            plan_code=account.plan_code,
            subscription_period_starts_at=account.subscription_period.starts_at,
            subscription_period_ends_at=account.subscription_period.ends_at,
            telegram_link=TelegramLinkStatusPayload(
                state=view.telegram_link.state.value,
                expires_at=view.telegram_link.expires_at,
            ),
            csrf_token=view.csrf_token,
            session_expires_at=view.session_expires_at,
        )


class TelegramLinkPayload(BaseModel):
    state: str
    url: str | None = None
    expires_at: datetime | None = None

    @classmethod
    def from_view(cls, view: TelegramLinkView) -> "TelegramLinkPayload":
        return cls(
            state=view.state,
            url=view.url,
            expires_at=view.expires_at,
        )


class PlanPayload(BaseModel):
    code: str
    display_name: str
    monthly_price_ars: Decimal
    currency: str
    billing_interval: str
    conversation_limit: int
    notification_limit: int

    @classmethod
    def from_plan(cls, plan: PublicPlan) -> "PlanPayload":
        return cls(
            code=plan.code,
            display_name=plan.display_name,
            monthly_price_ars=plan.monthly_price,
            currency=plan.currency,
            billing_interval=plan.billing_interval,
            conversation_limit=plan.conversation_limit,
            notification_limit=plan.notification_limit,
        )


class CheckoutRequestPayload(BaseModel):
    plan_code: Literal["basic", "max"]


class CheckoutPayload(BaseModel):
    subscription_id: UUID
    plan_code: str
    status: str
    checkout_url: str

    @classmethod
    def from_checkout(cls, checkout: PaymentCheckout) -> "CheckoutPayload":
        return cls(
            subscription_id=checkout.subscription.id,
            plan_code=checkout.subscription.plan_code,
            status=checkout.subscription.status.value,
            checkout_url=checkout.url,
        )


class SubscriptionPayload(BaseModel):
    plan: PlanPayload
    pending_plan: PlanPayload | None
    status: str
    period_starts_at: datetime
    period_ends_at: datetime
    provider_status: str | None
    checkout_url: str | None
    cancel_at_period_end: bool
    next_payment_at: datetime | None

    @classmethod
    def from_overview(
        cls,
        overview: SubscriptionOverview,
    ) -> "SubscriptionPayload":
        return cls(
            plan=PlanPayload.from_plan(overview.plan),
            pending_plan=(
                PlanPayload.from_plan(overview.pending_plan)
                if overview.pending_plan is not None
                else None
            ),
            status=overview.status,
            period_starts_at=overview.period.starts_at,
            period_ends_at=overview.period.ends_at,
            provider_status=overview.provider_status,
            checkout_url=overview.checkout_url,
            cancel_at_period_end=overview.cancel_at_period_end,
            next_payment_at=overview.next_payment_at,
        )
