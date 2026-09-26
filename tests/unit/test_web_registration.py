import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import UUID, uuid4

import pytest

from harle_domain.accounts import (
    AccountRepository,
    BrowserSession,
    GoogleIdentity,
    GoogleRegistration,
    SubscriptionPeriod,
    TelegramLinkCommandRecord,
    TelegramLinkOutcome,
    TelegramLinkRepository,
    TelegramLinkResult,
    WebAccountRepository,
)
from harle_domain.conversations.ports import ConversationUsageRepository
from harle_domain.messaging import (
    OutboundMessenger,
    TelegramUpdateReceipt,
    TelegramUpdateRepository,
    TelegramUpdateState,
)
from harle_services.access import PreflightService
from harle_services.accounts import (
    GoogleAuthService,
    SessionService,
    TelegramLinkCommand,
    TelegramLinkCommandDisposition,
    TelegramLinkCommandService,
    TelegramLinkService,
    advance_monthly_period,
)
from harle_services.messaging import MessageCoordinator, MessageFragment
from harle_utils import InvalidCsrfError, MessageDeliveryError

NOW = datetime(2026, 1, 31, 12, tzinfo=timezone.utc)
SECRET = "a-secure-signing-secret-with-32-characters"


class FakeGoogleProvider:
    def __init__(self) -> None:
        self.state = ""
        self.nonce = ""

    def authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
    ) -> str:
        self.state = state
        self.nonce = nonce
        return f"https://google.test?state={state}&challenge={code_challenge}"

    async def exchange(
        self,
        *,
        code: str,
        code_verifier: str,
        expected_nonce: str,
    ) -> GoogleIdentity:
        assert code == "authorization-code"
        assert code_verifier
        assert expected_nonce == self.nonce
        return GoogleIdentity(
            "google-subject",
            "New User",
            "user@example.com",
            True,
        )


class FakeWebAccounts:
    def __init__(self) -> None:
        self.user_id = uuid4()
        self.registrations: list[GoogleRegistration] = []

    async def find_or_create_google_user(
        self,
        *,
        registration: GoogleRegistration,
    ) -> UUID:
        if not self.registrations:
            self.registrations.append(registration)
        return self.user_id


class FakeSessions:
    def __init__(self) -> None:
        self.sessions: dict[str, BrowserSession] = {}

    async def create(
        self,
        *,
        user_id: UUID,
        token_hash: str,
        expires_at: datetime,
        created_at: datetime,
    ) -> BrowserSession:
        session = BrowserSession(uuid4(), user_id, expires_at, created_at)
        self.sessions[token_hash] = session
        return session

    async def resolve(
        self,
        *,
        token_hash: str,
        current_time: datetime,
    ) -> BrowserSession | None:
        session = self.sessions.get(token_hash)
        if session is None or session.expires_at <= current_time:
            return None
        return session

    async def revoke(
        self,
        *,
        token_hash: str,
        revoked_at: datetime,
    ) -> bool:
        del revoked_at
        return self.sessions.pop(token_hash, None) is not None


def test_google_login_creates_one_account_and_multiple_sessions() -> None:
    async def verify() -> None:
        provider = FakeGoogleProvider()
        accounts = FakeWebAccounts()
        sessions = SessionService(FakeSessions(), SECRET, clock=lambda: NOW)
        service = GoogleAuthService(
            provider=provider,
            accounts=cast(WebAccountRepository, accounts),
            sessions=sessions,
            signing_secret=SECRET,
            clock=lambda: NOW,
        )

        first_start = service.start(locale="es-AR", timezone_name="UTC")
        first = await service.complete(
            code="authorization-code",
            state=provider.state,
            state_cookie=first_start.state_cookie,
        )
        second_start = service.start(locale="es-AR", timezone_name="UTC")
        second = await service.complete(
            code="authorization-code",
            state=provider.state,
            state_cookie=second_start.state_cookie,
        )

        assert first.user_id == second.user_id == accounts.user_id
        assert len(accounts.registrations) == 1
        assert first.issued_session.token != second.issued_session.token

    asyncio.run(verify())


def test_session_csrf_and_monthly_period_rollover() -> None:
    async def verify() -> None:
        sessions = SessionService(FakeSessions(), SECRET, clock=lambda: NOW)
        issued = await sessions.issue(user_id=uuid4())
        resolved = await sessions.resolve(issued.token)

        assert resolved.id == issued.session.id
        sessions.require_csrf(
            session_token=issued.token,
            csrf_token=issued.csrf_token,
        )
        with pytest.raises(InvalidCsrfError):
            sessions.require_csrf(
                session_token=issued.token,
                csrf_token="invalid",
            )

    asyncio.run(verify())
    period = SubscriptionPeriod(
        starts_at=NOW,
        ends_at=datetime(2026, 2, 28, 12, tzinfo=timezone.utc),
    )
    renewed = advance_monthly_period(
        period,
        datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    assert renewed.starts_at == datetime(2026, 2, 28, 12, tzinfo=timezone.utc)
    assert renewed.ends_at == datetime(2026, 3, 31, 12, tzinfo=timezone.utc)


class FakeLinkRepository:
    def __init__(self, updates: "FakeUpdates") -> None:
        self.updates = updates
        self.processed = 0

    async def process_command(
        self,
        *,
        command: TelegramLinkCommandRecord,
    ) -> TelegramLinkResult:
        self.processed += 1
        if self.updates.states[command.update_id] is TelegramUpdateState.DELIVERED:
            return TelegramLinkResult(TelegramLinkOutcome.DUPLICATE)
        self.updates.states[command.update_id] = TelegramUpdateState.DELIVERED
        return TelegramLinkResult(TelegramLinkOutcome.LINKED, uuid4())


class FakeUpdates:
    def __init__(self) -> None:
        self.states: dict[int, TelegramUpdateState] = {}

    async def receive(
        self,
        *,
        update_id: int,
        telegram_user_id: int,
        telegram_chat_id: int,
        message_text: str,
    ) -> TelegramUpdateReceipt:
        del telegram_user_id, telegram_chat_id, message_text
        newly_persisted = update_id not in self.states
        state = self.states.setdefault(update_id, TelegramUpdateState.RECEIVED)
        return TelegramUpdateReceipt(state, newly_persisted)

    async def mark_rate_limited(self, update_ids: list[int]) -> None:
        for update_id in update_ids:
            self.states[update_id] = TelegramUpdateState.RATE_LIMITED


class FakeMessenger:
    def __init__(self, *, fail_delivery: bool = False) -> None:
        self.messages: list[str] = []
        self.fail_delivery = fail_delivery

    async def send_message(self, *, chat_id: int, text: str) -> None:
        del chat_id
        if self.fail_delivery:
            raise MessageDeliveryError
        self.messages.append(text)


@dataclass(frozen=True)
class LinkHarness:
    service: TelegramLinkCommandService
    repository: FakeLinkRepository
    preflight: PreflightService


def _link_commands(
    updates: FakeUpdates,
    messenger: FakeMessenger,
) -> LinkHarness:
    repository = FakeLinkRepository(updates)
    preflight = PreflightService(
        cast(AccountRepository, object()),
        cast(ConversationUsageRepository, object()),
        clock=lambda: NOW,
    )
    service = TelegramLinkCommandService(
        links=TelegramLinkService(
            repository=cast(TelegramLinkRepository, repository),
            bot_username="harle_bot",
            clock=lambda: NOW + timedelta(minutes=1),
        ),
        updates=cast(TelegramUpdateRepository, updates),
        messenger=cast(OutboundMessenger, messenger),
        rate_limiter=preflight.check_rate_limit,
    )
    return LinkHarness(service, repository, preflight)


def _command(update_id: int) -> TelegramLinkCommand:
    return TelegramLinkCommand(
        update_id=update_id,
        telegram_user_id=200,
        telegram_chat_id=300,
        telegram_display_name="User",
        token="valid-token",
    )


def test_link_commands_apply_shared_deduplicated_safety_limit() -> None:
    async def verify() -> None:
        updates = FakeUpdates()
        messenger = FakeMessenger()
        harness = _link_commands(updates, messenger)

        first = await harness.service.handle(command=_command(1))
        duplicate = await harness.service.handle(command=_command(1))
        results = [
            await harness.service.handle(command=_command(update_id))
            for update_id in range(2, 12)
        ]

        assert first.disposition is TelegramLinkCommandDisposition.PROCESSED
        assert duplicate.disposition is TelegramLinkCommandDisposition.DUPLICATE
        assert results[8].disposition is TelegramLinkCommandDisposition.RATE_LIMITED
        assert results[9].disposition is TelegramLinkCommandDisposition.RATE_LIMITED
        assert harness.repository.processed == 9
        cooldowns = [
            message for message in messenger.messages if "too quickly" in message
        ]
        assert len(cooldowns) == 1

    asyncio.run(verify())


def test_normal_messages_and_link_commands_share_safety_state() -> None:
    async def verify() -> None:
        updates = FakeUpdates()
        messenger = FakeMessenger()
        harness = _link_commands(updates, messenger)
        coordinator = MessageCoordinator(
            cast(TelegramUpdateRepository, updates),
            harness.preflight.check_rate_limit,
        )
        for update_id in range(1, 10):
            await coordinator.receive(
                MessageFragment(
                    update_id=update_id,
                    telegram_user_id=200,
                    telegram_chat_id=300,
                    text="hello",
                ),
            )

        result = await harness.service.handle(command=_command(10))

        assert result.disposition is TelegramLinkCommandDisposition.RATE_LIMITED
        assert harness.repository.processed == 0
        assert len(messenger.messages) == 1

    asyncio.run(verify())


def test_delivery_failure_does_not_change_completed_link() -> None:
    async def verify() -> None:
        updates = FakeUpdates()
        harness = _link_commands(
            updates,
            FakeMessenger(fail_delivery=True),
        )

        first = await harness.service.handle(command=_command(1))
        duplicate = await harness.service.handle(command=_command(1))

        assert first.disposition is TelegramLinkCommandDisposition.PROCESSED
        assert duplicate.disposition is TelegramLinkCommandDisposition.DUPLICATE
        assert harness.repository.processed == 1

    asyncio.run(verify())
