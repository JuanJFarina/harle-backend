import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from hmac import compare_digest, digest
from secrets import token_urlsafe
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from harle_domain.accounts import (
    GoogleAuthentication,
    GoogleIdentityProvider,
    GoogleRegistration,
    OAuthAuthorization,
    WebAccountRepository,
)
from harle_utils import Clock, InvalidOAuthError, as_utc, utc_now

from .free_periods import first_monthly_period
from .sessions import SessionService

LOCALE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


@dataclass(frozen=True, slots=True)
class _OAuthAttempt:
    state: str
    nonce: str
    code_verifier: str
    locale: str
    timezone_name: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class GoogleAuthService:
    provider: GoogleIdentityProvider
    accounts: WebAccountRepository
    sessions: SessionService
    signing_secret: str
    state_lifetime: timedelta = timedelta(minutes=10)
    clock: Clock = utc_now

    def __post_init__(self) -> None:
        if len(self.signing_secret) < 32:
            raise ValueError(
                "OAuth signing secret must contain at least 32 characters.",
            )
        if self.state_lifetime <= timedelta(0):
            raise ValueError("OAuth state lifetime must be positive.")

    def start(self, *, locale: str, timezone_name: str) -> OAuthAuthorization:
        normalized_locale = _locale(locale)
        normalized_timezone = _timezone(timezone_name)
        current_time = as_utc(self.clock())
        attempt = _OAuthAttempt(
            state=token_urlsafe(32),
            nonce=token_urlsafe(32),
            code_verifier=token_urlsafe(48),
            locale=normalized_locale,
            timezone_name=normalized_timezone,
            expires_at=current_time + self.state_lifetime,
        )
        challenge = _base64url(sha256(attempt.code_verifier.encode()).digest())
        return OAuthAuthorization(
            url=self.provider.authorization_url(
                state=attempt.state,
                nonce=attempt.nonce,
                code_challenge=challenge,
            ),
            state_cookie=_encode_attempt(attempt, self.signing_secret),
            expires_at=attempt.expires_at,
        )

    async def complete(
        self,
        *,
        code: str,
        state: str,
        state_cookie: str | None,
    ) -> GoogleAuthentication:
        if not code or not state or state_cookie is None:
            raise InvalidOAuthError
        attempt = _decode_attempt(state_cookie, self.signing_secret)
        current_time = as_utc(self.clock())
        if attempt.expires_at <= current_time or not compare_digest(
            attempt.state,
            state,
        ):
            raise InvalidOAuthError
        identity = await self.provider.exchange(
            code=code,
            code_verifier=attempt.code_verifier,
            expected_nonce=attempt.nonce,
        )
        registration = GoogleRegistration(
            identity=identity,
            locale=attempt.locale,
            timezone=attempt.timezone_name,
            period=first_monthly_period(current_time),
            created_at=current_time,
        )
        user_id = await self.accounts.find_or_create_google_user(
            registration=registration,
        )
        issued_session = await self.sessions.issue(user_id=user_id)
        return GoogleAuthentication(
            user_id=user_id,
            issued_session=issued_session,
        )


def _encode_attempt(attempt: _OAuthAttempt, signing_secret: str) -> str:
    payload = json.dumps(
        {
            "state": attempt.state,
            "nonce": attempt.nonce,
            "code_verifier": attempt.code_verifier,
            "locale": attempt.locale,
            "timezone": attempt.timezone_name,
            "expires_at": int(attempt.expires_at.timestamp()),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = _base64url(payload)
    signature = digest(signing_secret.encode(), encoded.encode(), "sha256").hex()
    return f"{encoded}.{signature}"


def _decode_attempt(value: str, signing_secret: str) -> _OAuthAttempt:
    try:
        encoded, signature = value.split(".", maxsplit=1)
        expected = digest(
            signing_secret.encode(),
            encoded.encode(),
            "sha256",
        ).hex()
        if not compare_digest(signature, expected):
            raise InvalidOAuthError
        decoded = base64.urlsafe_b64decode(_base64_padding(encoded))
        raw_payload: object = json.loads(decoded)
        if not isinstance(raw_payload, Mapping):
            raise InvalidOAuthError
        state = _required_text(raw_payload, "state")
        nonce = _required_text(raw_payload, "nonce")
        code_verifier = _required_text(raw_payload, "code_verifier")
        locale = _locale(_required_text(raw_payload, "locale"))
        timezone_name = _timezone(_required_text(raw_payload, "timezone"))
        expires_at_value = raw_payload.get("expires_at")
        if not isinstance(expires_at_value, int):
            raise InvalidOAuthError
        expires_at = datetime.fromtimestamp(expires_at_value, tz=timezone.utc)
    except (
        binascii.Error,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise InvalidOAuthError from exc
    return _OAuthAttempt(
        state=state,
        nonce=nonce,
        code_verifier=code_verifier,
        locale=locale,
        timezone_name=timezone_name,
        expires_at=expires_at,
    )


def _required_text(payload: Mapping[object, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidOAuthError
    return value


def _locale(value: str) -> str:
    normalized = value.strip().replace("_", "-")
    if not LOCALE_PATTERN.fullmatch(normalized):
        raise InvalidOAuthError
    return normalized


def _timezone(value: str) -> str:
    normalized = value.strip()
    try:
        ZoneInfo(normalized)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise InvalidOAuthError from exc
    return normalized


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _base64_padding(value: str) -> str:
    return value + "=" * (-len(value) % 4)
