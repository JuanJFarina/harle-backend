from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from .models import SubscriptionPeriod


class PaymentSubscriptionStatus(str, Enum):
    CREATING = "creating"
    PENDING = "pending"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELLED = "cancelled"
    ENDED = "ended"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PublicPlan:
    code: str
    display_name: str
    monthly_price: Decimal
    currency: str
    billing_interval: str
    conversation_limit: int
    notification_limit: int


@dataclass(frozen=True, slots=True)
class PaymentSubscription:
    id: UUID
    user_id: UUID
    plan_code: str
    payer_email: str
    status: PaymentSubscriptionStatus
    provider_status: str
    provider_subscription_id: str | None
    provider_updated_at: datetime | None
    checkout_url: str | None
    period: SubscriptionPeriod | None
    cancel_at_period_end: bool
    next_payment_at: datetime | None
    latest_payment_at: datetime | None
    latest_payment_updated_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PaymentCheckout:
    subscription: PaymentSubscription
    url: str


@dataclass(frozen=True, slots=True)
class ProviderSubscription:
    id: str
    external_reference: UUID
    status: str
    checkout_url: str | None
    next_payment_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderAuthorizedPayment:
    invoice_id: str
    subscription_id: str
    external_reference: UUID
    payment_id: str | None
    payment_status: str
    amount: Decimal
    currency: str
    debit_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PaymentWebhook:
    event_id: str
    request_id: str
    topic: str
    resource_id: str
    signature: str


@dataclass(frozen=True, slots=True)
class SubscriptionOverview:
    plan: PublicPlan
    pending_plan: PublicPlan | None
    status: str
    period: SubscriptionPeriod
    provider_status: str | None
    checkout_url: str | None
    cancel_at_period_end: bool
    next_payment_at: datetime | None
