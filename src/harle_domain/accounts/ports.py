from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from harle_domain.accounts.models import ResolvedUser, SubscriptionPeriod
from harle_domain.accounts.payments import (
    PaymentSubscription,
    PaymentWebhook,
    ProviderAuthorizedPayment,
    ProviderSubscription,
    PublicPlan,
)
from harle_domain.accounts.web import (
    AccountOverview,
    BrowserSession,
    FreeAccountPeriod,
    GoogleIdentity,
    GoogleRegistration,
    TelegramLinkCommandRecord,
    TelegramLinkResult,
    TelegramLinkStatus,
)


@runtime_checkable
class AccountRepository(Protocol):
    async def resolve_telegram_identity(
        self,
        *,
        telegram_user_id: int,
    ) -> ResolvedUser | None: ...

    async def resolve_user_telegram_identity(
        self,
        *,
        user_id: UUID,
    ) -> ResolvedUser | None: ...


@runtime_checkable
class WebAccountRepository(Protocol):
    async def find_or_create_google_user(
        self,
        *,
        registration: GoogleRegistration,
    ) -> UUID: ...

    async def get_overview(self, *, user_id: UUID) -> AccountOverview | None: ...

    async def get_free_period(
        self,
        *,
        user_id: UUID,
        current_time: datetime,
    ) -> FreeAccountPeriod | None: ...

    async def list_due_free_periods(
        self,
        *,
        current_time: datetime,
        limit: int,
    ) -> Sequence[FreeAccountPeriod]: ...

    async def replace_free_period(
        self,
        *,
        user_id: UUID,
        expected_period: SubscriptionPeriod,
        new_period: SubscriptionPeriod,
        synchronized_at: datetime,
    ) -> bool: ...


@runtime_checkable
class BrowserSessionRepository(Protocol):
    async def create(
        self,
        *,
        user_id: UUID,
        token_hash: str,
        expires_at: datetime,
        created_at: datetime,
    ) -> BrowserSession: ...

    async def resolve(
        self,
        *,
        token_hash: str,
        current_time: datetime,
    ) -> BrowserSession | None: ...

    async def revoke(
        self,
        *,
        token_hash: str,
        revoked_at: datetime,
    ) -> bool: ...


@runtime_checkable
class TelegramLinkRepository(Protocol):
    async def issue(
        self,
        *,
        user_id: UUID,
        token_hash: str,
        expires_at: datetime,
        created_at: datetime,
    ) -> None: ...

    async def get_status(
        self,
        *,
        user_id: UUID,
        current_time: datetime,
    ) -> TelegramLinkStatus: ...

    async def process_command(
        self,
        *,
        command: TelegramLinkCommandRecord,
    ) -> TelegramLinkResult: ...


@runtime_checkable
class GoogleIdentityProvider(Protocol):
    def authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
    ) -> str: ...

    async def exchange(
        self,
        *,
        code: str,
        code_verifier: str,
        expected_nonce: str,
    ) -> GoogleIdentity: ...


@runtime_checkable
class PaymentSubscriptionRepository(Protocol):
    async def list_public_plans(self) -> Sequence[PublicPlan]: ...

    async def get_public_plan(self, *, code: str) -> PublicPlan | None: ...

    async def get_verified_email(self, *, user_id: UUID) -> str | None: ...

    async def begin_checkout(
        self,
        *,
        user_id: UUID,
        plan_code: str,
        payer_email: str,
        created_at: datetime,
    ) -> PaymentSubscription: ...

    async def attach_provider_subscription(
        self,
        *,
        subscription_id: UUID,
        provider: ProviderSubscription,
        updated_at: datetime,
    ) -> PaymentSubscription: ...

    async def fail_checkout(
        self,
        *,
        subscription_id: UUID,
        updated_at: datetime,
    ) -> None: ...

    async def get_open_for_user(
        self,
        *,
        user_id: UUID,
    ) -> PaymentSubscription | None: ...

    async def get_by_provider_id(
        self,
        *,
        provider_subscription_id: str,
    ) -> PaymentSubscription | None: ...

    async def reconcile_provider_subscription(
        self,
        *,
        provider: ProviderSubscription,
        updated_at: datetime,
    ) -> PaymentSubscription | None: ...

    async def reconcile_authorized_payment(
        self,
        *,
        payment: ProviderAuthorizedPayment,
        period: SubscriptionPeriod | None,
        updated_at: datetime,
    ) -> PaymentSubscription | None: ...

    async def cancel_at_period_end(
        self,
        *,
        subscription_id: UUID,
        provider_status: str,
        updated_at: datetime,
    ) -> PaymentSubscription: ...

    async def expire_user_paid_subscription(
        self,
        *,
        user_id: UUID,
        current_time: datetime,
        free_period: SubscriptionPeriod,
    ) -> bool: ...

    async def expire_due_paid_subscriptions(
        self,
        *,
        current_time: datetime,
        free_period: SubscriptionPeriod,
        limit: int,
    ) -> int: ...

    async def claim_webhook(
        self,
        *,
        webhook: PaymentWebhook,
        received_at: datetime,
    ) -> bool: ...

    async def complete_webhook(
        self,
        *,
        event_id: str,
        processed_at: datetime,
    ) -> None: ...

    async def fail_webhook(
        self,
        *,
        event_id: str,
        failed_at: datetime,
    ) -> None: ...


@runtime_checkable
class PaymentProvider(Protocol):
    async def create_subscription(
        self,
        *,
        external_reference: UUID,
        plan: PublicPlan,
        payer_email: str,
        back_url: str,
    ) -> ProviderSubscription: ...

    async def get_subscription(
        self,
        *,
        subscription_id: str,
    ) -> ProviderSubscription: ...

    async def get_authorized_payment(
        self,
        *,
        invoice_id: str,
    ) -> ProviderAuthorizedPayment: ...

    async def cancel_subscription(
        self,
        *,
        subscription_id: str,
    ) -> ProviderSubscription: ...

    def verify_webhook(self, webhook: PaymentWebhook) -> None: ...
