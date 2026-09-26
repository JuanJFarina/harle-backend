from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID

from .models import SubscriptionPeriod, SubscriptionStatus


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    subject: str
    display_name: str
    email: str
    email_verified: bool

    def __post_init__(self) -> None:
        if not self.subject.strip():
            raise ValueError("Google subject cannot be empty.")
        if not self.display_name.strip():
            raise ValueError("Google display name cannot be empty.")
        normalized_email = self.email.strip().lower()
        if "@" not in normalized_email:
            raise ValueError("Google email is invalid.")
        if not self.email_verified:
            raise ValueError("Google email must be verified.")
        object.__setattr__(self, "email", normalized_email)


@dataclass(frozen=True, slots=True)
class GoogleRegistration:
    identity: GoogleIdentity
    locale: str
    timezone: str
    period: SubscriptionPeriod
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FreeAccountPeriod:
    user_id: UUID
    period: SubscriptionPeriod


@dataclass(frozen=True, slots=True)
class AccountOverview:
    user_id: UUID
    display_name: str
    plan_code: str
    subscription_status: SubscriptionStatus
    subscription_period: SubscriptionPeriod
    telegram_linked: bool


@dataclass(frozen=True, slots=True)
class BrowserSession:
    id: UUID
    user_id: UUID
    expires_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedSession:
    session: BrowserSession
    token: str
    csrf_token: str


class TelegramLinkState(str, Enum):
    DISCONNECTED = "disconnected"
    PENDING = "pending"
    CONNECTED = "connected"


@dataclass(frozen=True, slots=True)
class TelegramLinkStatus:
    state: TelegramLinkState
    expires_at: datetime | None = None


class TelegramLinkOutcome(str, Enum):
    LINKED = "linked"
    ALREADY_LINKED = "already_linked"
    INVALID = "invalid"
    CONFLICT = "conflict"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class TelegramLinkResult:
    outcome: TelegramLinkOutcome
    user_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class TelegramLinkCommandRecord:
    update_id: int
    telegram_user_id: int
    telegram_chat_id: int
    telegram_display_name: str
    token_hash: str
    processed_at: datetime


@dataclass(frozen=True, slots=True)
class OAuthAuthorization:
    url: str
    state_cookie: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class GoogleAuthentication:
    user_id: UUID
    issued_session: IssuedSession
