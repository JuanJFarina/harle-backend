from dataclasses import dataclass
from datetime import datetime

from harle_domain.accounts import (
    AccountOverview,
    TelegramLinkRepository,
    TelegramLinkStatus,
    WebAccountRepository,
)
from harle_utils import AuthenticationRequiredError, Clock, as_utc, utc_now

from .sessions import SessionService
from .subscriptions import SubscriptionMaintenanceService


@dataclass(frozen=True, slots=True)
class WebSessionResult:
    account: AccountOverview
    telegram_link: TelegramLinkStatus
    csrf_token: str
    session_expires_at: datetime


@dataclass(frozen=True, slots=True)
class WebAccountService:
    accounts: WebAccountRepository
    sessions: SessionService
    telegram_links: TelegramLinkRepository
    subscriptions: SubscriptionMaintenanceService
    clock: Clock = utc_now

    async def get_session(self, session_token: str | None) -> WebSessionResult:
        if session_token is None:
            raise AuthenticationRequiredError
        session = await self.sessions.resolve(session_token)
        await self.subscriptions.ensure_user(session.user_id)
        account = await self.accounts.get_overview(user_id=session.user_id)
        if account is None:
            raise AuthenticationRequiredError
        telegram_link = await self.telegram_links.get_status(
            user_id=session.user_id,
            current_time=as_utc(self.clock()),
        )
        return WebSessionResult(
            account=account,
            telegram_link=telegram_link,
            csrf_token=self.sessions.csrf_token(session_token),
            session_expires_at=session.expires_at,
        )
