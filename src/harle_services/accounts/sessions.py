from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from hmac import compare_digest, digest
from secrets import token_urlsafe
from uuid import UUID

from harle_domain.accounts import (
    BrowserSession,
    BrowserSessionRepository,
    IssuedSession,
)
from harle_utils import (
    AuthenticationRequiredError,
    Clock,
    InvalidCsrfError,
    as_utc,
    utc_now,
)


@dataclass(frozen=True, slots=True)
class SessionService:
    repository: BrowserSessionRepository
    signing_secret: str
    lifetime: timedelta = timedelta(days=30)
    clock: Clock = utc_now

    def __post_init__(self) -> None:
        if len(self.signing_secret) < 32:
            raise ValueError(
                "Session signing secret must contain at least 32 characters.",
            )
        if self.lifetime <= timedelta(0):
            raise ValueError("Session lifetime must be positive.")

    async def issue(self, *, user_id: UUID) -> IssuedSession:
        token = token_urlsafe(32)
        created_at = as_utc(self.clock())
        session = await self.repository.create(
            user_id=user_id,
            token_hash=hash_token(token),
            expires_at=created_at + self.lifetime,
            created_at=created_at,
        )
        return IssuedSession(
            session=session,
            token=token,
            csrf_token=self.csrf_token(token),
        )

    async def resolve(self, token: str | None) -> BrowserSession:
        if token is None or not token:
            raise AuthenticationRequiredError
        session = await self.repository.resolve(
            token_hash=hash_token(token),
            current_time=as_utc(self.clock()),
        )
        if session is None:
            raise AuthenticationRequiredError
        return session

    async def revoke(self, token: str | None) -> None:
        if token is None or not token:
            return
        await self.repository.revoke(
            token_hash=hash_token(token),
            revoked_at=as_utc(self.clock()),
        )

    def require_csrf(self, *, session_token: str, csrf_token: str | None) -> None:
        if csrf_token is None or not compare_digest(
            csrf_token,
            self.csrf_token(session_token),
        ):
            raise InvalidCsrfError

    def csrf_token(self, session_token: str) -> str:
        return digest(
            self.signing_secret.encode(),
            f"csrf:{session_token}".encode(),
            "sha256",
        ).hex()


def hash_token(token: str) -> str:
    return sha256(token.encode()).hexdigest()
