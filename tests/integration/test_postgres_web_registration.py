import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import asyncpg
import pytest

from harle_domain.accounts import (
    GoogleIdentity,
    GoogleRegistration,
    TelegramLinkOutcome,
)
from harle_infrastructure.postgres import (
    PostgresAccountRepository,
    PostgresBrowserSessionRepository,
    PostgresConversationRepository,
    PostgresTelegramLinkRepository,
    PostgresTelegramUpdateRepository,
    PostgresWebAccountRepository,
    create_postgres_pool,
    validate_postgres_schema,
)
from harle_services.access import PreflightAccepted, PreflightService
from harle_services.accounts import (
    FreeSubscriptionService,
    SessionService,
    TelegramLinkCommand,
    TelegramLinkService,
    first_monthly_period,
)

DATABASE_URL = os.environ.get("TEST_POSTGRES_DATABASE_URL")
ROOT = Path(__file__).parents[2]
SCHEMA_PATHS = (
    ROOT / "scripts" / "apply_multi_user_runtime.sql",
    ROOT / "scripts" / "apply_subscription_interactions.sql",
    ROOT / "scripts" / "apply_interaction_frequency.sql",
    ROOT / "scripts" / "apply_internal_expenses.sql",
    ROOT / "scripts" / "apply_internal_events.sql",
    ROOT / "scripts" / "apply_event_notification_quotas.sql",
    ROOT / "scripts" / "apply_telegram_dedup_ordering.sql",
    ROOT / "scripts" / "apply_bans_quotas.sql",
    ROOT / "scripts" / "apply_web_registration.sql",
    ROOT / "scripts" / "apply_paid_subscriptions.sql",
)


async def _apply_schema_twice(database_url: str) -> None:
    connection = await asyncpg.connect(database_url)
    try:
        for _ in range(2):
            for schema_path in SCHEMA_PATHS:
                await connection.execute(schema_path.read_text(encoding="utf-8"))
    finally:
        await connection.close()


async def _create_google_user(
    repository: PostgresWebAccountRepository,
    *,
    display_name: str,
    created_at: datetime,
) -> UUID:
    return await repository.find_or_create_google_user(
        registration=GoogleRegistration(
            identity=GoogleIdentity(
                subject=f"google-{uuid4()}",
                display_name=display_name,
                email=f"{uuid4()}@example.com",
                email_verified=True,
            ),
            locale="es-AR",
            timezone="UTC",
            period=first_monthly_period(created_at),
            created_at=created_at,
        ),
    )


def _link_token(url: str | None) -> str:
    assert url is not None
    start_values = parse_qs(urlsplit(url).query).get("start")
    assert start_values
    return start_values[0]


async def _claim_link_update(
    repository: PostgresTelegramUpdateRepository,
    *,
    update_id: int,
    telegram_user_id: int,
) -> None:
    await repository.receive(
        update_id=update_id,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_user_id,
        message_text="[Telegram account link]",
    )


async def verify_web_registration(database_url: str) -> None:
    await _apply_schema_twice(database_url)

    pool = await create_postgres_pool(
        database_url=database_url,
        min_size=1,
        max_size=2,
    )
    user_id = None
    now = datetime.now(timezone.utc)
    telegram_user_id = uuid4().int % 8_000_000_000 + 1
    try:
        await validate_postgres_schema(pool)
        web_accounts = PostgresWebAccountRepository(pool)
        registration = GoogleRegistration(
            identity=GoogleIdentity(
                subject=f"google-{uuid4()}",
                display_name="Web User",
                email=f"{uuid4()}@example.com",
                email_verified=True,
            ),
            locale="es-AR",
            timezone="UTC",
            period=first_monthly_period(now),
            created_at=now,
        )
        user_id = await web_accounts.find_or_create_google_user(
            registration=registration,
        )
        repeated_id = await web_accounts.find_or_create_google_user(
            registration=registration,
        )
        assert repeated_id == user_id

        sessions = SessionService(
            PostgresBrowserSessionRepository(pool),
            "integration-signing-secret-with-32-characters",
        )
        issued = await sessions.issue(user_id=user_id)
        assert (await sessions.resolve(issued.token)).user_id == user_id

        links = TelegramLinkService(
            PostgresTelegramLinkRepository(pool),
            "harle_test_bot",
        )
        issued_link = await links.issue(user_id=user_id)
        token = _link_token(issued_link.url)
        telegram_updates = PostgresTelegramUpdateRepository(pool)
        invalid_id = telegram_user_id + 1
        await _claim_link_update(
            telegram_updates,
            update_id=invalid_id,
            telegram_user_id=invalid_id,
        )
        invalid_command = TelegramLinkCommand(
            update_id=invalid_id,
            telegram_user_id=invalid_id,
            telegram_chat_id=invalid_id,
            telegram_display_name="Invalid Telegram User",
            token="invalid-token",
        )
        invalid = await links.process_command(command=invalid_command)
        assert invalid.outcome is TelegramLinkOutcome.INVALID
        invalid_duplicate = await links.process_command(command=invalid_command)
        assert invalid_duplicate.outcome is TelegramLinkOutcome.DUPLICATE
        await _claim_link_update(
            telegram_updates,
            update_id=telegram_user_id,
            telegram_user_id=telegram_user_id,
        )
        command = TelegramLinkCommand(
            update_id=telegram_user_id,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_user_id,
            telegram_display_name="Telegram User",
            token=token,
        )
        result = await links.process_command(command=command)
        assert result.user_id == user_id
        duplicate = await links.process_command(command=command)
        assert duplicate.outcome is TelegramLinkOutcome.DUPLICATE
        async with pool.acquire() as connection:
            await connection.execute(
                (ROOT / "scripts" / "apply_web_registration.sql").read_text(
                    encoding="utf-8",
                ),
            )

        conversations = PostgresConversationRepository(pool)
        preflight = PreflightService(
            PostgresAccountRepository(pool),
            conversations,
            FreeSubscriptionService(web_accounts),
        )
        accepted = await preflight.check(telegram_user_id)
        assert isinstance(accepted, PreflightAccepted)
        assert accepted.resolved_user.user.id == user_id
        await preflight.release(accepted.quota_reservation)
        overview = await web_accounts.get_overview(user_id=user_id)
        assert overview is not None and overview.telegram_linked
    finally:
        await pool.close()
        if user_id is not None:
            cleanup = await asyncpg.connect(database_url)
            try:
                await cleanup.execute(
                    "DELETE FROM telegram_update_claims WHERE update_id = ANY($1::bigint[])",
                    [telegram_user_id, telegram_user_id + 1],
                )
                await cleanup.execute("DELETE FROM users WHERE id = $1", user_id)
            finally:
                await cleanup.close()


@pytest.mark.skipif(
    DATABASE_URL is None,
    reason="TEST_POSTGRES_DATABASE_URL is not configured",
)
def test_google_registration_links_telegram_and_passes_preflight() -> None:
    assert DATABASE_URL is not None
    asyncio.run(verify_web_registration(DATABASE_URL))


async def verify_concurrent_link_issuance(database_url: str) -> None:
    await _apply_schema_twice(database_url)
    pool = await create_postgres_pool(
        database_url=database_url,
        min_size=1,
        max_size=4,
    )
    user_id = None
    telegram_update_ids: list[int] = []
    try:
        accounts = PostgresWebAccountRepository(pool)
        user_id = await _create_google_user(
            accounts,
            display_name="Concurrent Link User",
            created_at=datetime.now(timezone.utc),
        )
        links = TelegramLinkService(
            PostgresTelegramLinkRepository(pool),
            "harle_test_bot",
        )
        first, second = await asyncio.gather(
            links.issue(user_id=user_id),
            links.issue(user_id=user_id),
        )
        latest = await links.issue(user_id=user_id)
        async with pool.acquire() as connection:
            pending = await connection.fetchval(
                """
                SELECT COUNT(*)
                FROM telegram_link_tokens
                WHERE user_id = $1
                    AND consumed_at IS NULL
                """,
                user_id,
            )
        assert pending == 1

        updates = PostgresTelegramUpdateRepository(pool)
        telegram_base = uuid4().int % 8_000_000_000 + 1
        outcomes = []
        for offset, issued in enumerate((first, second, latest), start=1):
            telegram_user_id = telegram_base + offset
            telegram_update_ids.append(telegram_user_id)
            await _claim_link_update(
                updates,
                update_id=telegram_user_id,
                telegram_user_id=telegram_user_id,
            )
            result = await links.process_command(
                command=TelegramLinkCommand(
                    update_id=telegram_user_id,
                    telegram_user_id=telegram_user_id,
                    telegram_chat_id=telegram_user_id,
                    telegram_display_name="Concurrent Telegram User",
                    token=_link_token(issued.url),
                ),
            )
            outcomes.append(result.outcome)

        assert outcomes.count(TelegramLinkOutcome.INVALID) == 2
        assert outcomes.count(TelegramLinkOutcome.LINKED) == 1
        async with pool.acquire() as connection:
            pending = await connection.fetchval(
                """
                SELECT COUNT(*)
                FROM telegram_link_tokens
                WHERE user_id = $1
                    AND consumed_at IS NULL
                """,
                user_id,
            )
        assert pending == 0
    finally:
        await pool.close()
        if user_id is not None:
            cleanup = await asyncpg.connect(database_url)
            try:
                await cleanup.execute(
                    "DELETE FROM telegram_update_claims WHERE update_id = ANY($1::bigint[])",
                    telegram_update_ids,
                )
                await cleanup.execute("DELETE FROM users WHERE id = $1", user_id)
            finally:
                await cleanup.close()


@pytest.mark.skipif(
    DATABASE_URL is None,
    reason="TEST_POSTGRES_DATABASE_URL is not configured",
)
def test_concurrent_link_issuance_keeps_one_pending_token() -> None:
    assert DATABASE_URL is not None
    asyncio.run(verify_concurrent_link_issuance(DATABASE_URL))


async def _set_failure_trigger(
    pool: asyncpg.Pool,
    *,
    update_id: int,
    telegram_user_id: int | None,
) -> None:
    async with pool.acquire() as connection:
        if telegram_user_id is None:
            await connection.execute(
                f"""
                CREATE OR REPLACE FUNCTION test_fail_link_command()
                RETURNS TRIGGER
                LANGUAGE plpgsql
                AS $$
                BEGIN
                    IF NEW.update_id = {update_id}
                        AND NEW.status = 'processing' THEN
                        RAISE EXCEPTION 'forced link processing failure';
                    END IF;
                    RETURN NEW;
                END
                $$;

                DROP TRIGGER IF EXISTS test_fail_link_command
                ON telegram_update_claims;

                CREATE TRIGGER test_fail_link_command
                BEFORE UPDATE OF status ON telegram_update_claims
                FOR EACH ROW
                EXECUTE FUNCTION test_fail_link_command();
                """,
            )
            return
        await connection.execute(
            f"""
            CREATE OR REPLACE FUNCTION test_fail_link_identity()
            RETURNS TRIGGER
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NEW.provider = 'telegram'
                    AND NEW.external_user_id = '{telegram_user_id}' THEN
                    RAISE EXCEPTION 'forced link identity failure';
                END IF;
                RETURN NEW;
            END
            $$;

            DROP TRIGGER IF EXISTS test_fail_link_identity
            ON external_identities;

            CREATE TRIGGER test_fail_link_identity
            BEFORE INSERT ON external_identities
            FOR EACH ROW
            EXECUTE FUNCTION test_fail_link_identity();
            """,
        )


async def _drop_failure_triggers(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as connection:
        await connection.execute(
            """
            DROP TRIGGER IF EXISTS test_fail_link_command
            ON telegram_update_claims;
            DROP TRIGGER IF EXISTS test_fail_link_identity
            ON external_identities;
            DROP FUNCTION IF EXISTS test_fail_link_command();
            DROP FUNCTION IF EXISTS test_fail_link_identity();
            """,
        )


async def verify_transactional_link_retry(database_url: str) -> None:
    await _apply_schema_twice(database_url)
    pool = await create_postgres_pool(
        database_url=database_url,
        min_size=1,
        max_size=3,
    )
    user_ids: list[UUID] = []
    telegram_update_ids: list[int] = []
    try:
        accounts = PostgresWebAccountRepository(pool)
        link_repository = PostgresTelegramLinkRepository(pool)
        links = TelegramLinkService(link_repository, "harle_test_bot")
        updates = PostgresTelegramUpdateRepository(pool)

        before_user = await _create_google_user(
            accounts,
            display_name="Before Failure User",
            created_at=datetime.now(timezone.utc),
        )
        user_ids.append(before_user)
        before_link = await links.issue(user_id=before_user)
        before_id = uuid4().int % 8_000_000_000 + 1
        telegram_update_ids.append(before_id)
        await _claim_link_update(
            updates,
            update_id=before_id,
            telegram_user_id=before_id,
        )
        before_command = TelegramLinkCommand(
            update_id=before_id,
            telegram_user_id=before_id,
            telegram_chat_id=before_id,
            telegram_display_name="Before Failure Telegram",
            token=_link_token(before_link.url),
        )
        await _set_failure_trigger(
            pool,
            update_id=before_id,
            telegram_user_id=None,
        )
        with pytest.raises(asyncpg.PostgresError):
            await links.process_command(command=before_command)
        await _drop_failure_triggers(pool)
        await _assert_retryable(pool, before_user, before_id)
        before_result = await links.process_command(command=before_command)
        assert before_result.outcome is TelegramLinkOutcome.LINKED

        after_user = await _create_google_user(
            accounts,
            display_name="After Failure User",
            created_at=datetime.now(timezone.utc),
        )
        user_ids.append(after_user)
        after_link = await links.issue(user_id=after_user)
        after_id = before_id + 10
        telegram_update_ids.append(after_id)
        await _claim_link_update(
            updates,
            update_id=after_id,
            telegram_user_id=after_id,
        )
        after_command = TelegramLinkCommand(
            update_id=after_id,
            telegram_user_id=after_id,
            telegram_chat_id=after_id,
            telegram_display_name="After Failure Telegram",
            token=_link_token(after_link.url),
        )
        await _set_failure_trigger(
            pool,
            update_id=after_id,
            telegram_user_id=after_id,
        )
        with pytest.raises(asyncpg.PostgresError):
            await links.process_command(command=after_command)
        await _drop_failure_triggers(pool)
        await _assert_retryable(pool, after_user, after_id)
        after_result = await links.process_command(command=after_command)
        assert after_result.outcome is TelegramLinkOutcome.LINKED

        conflict_user = await _create_google_user(
            accounts,
            display_name="Conflict User",
            created_at=datetime.now(timezone.utc),
        )
        user_ids.append(conflict_user)
        conflict_link = await links.issue(user_id=conflict_user)
        conflict_update = after_id + 10
        telegram_update_ids.append(conflict_update)
        await _claim_link_update(
            updates,
            update_id=conflict_update,
            telegram_user_id=after_id,
        )
        conflict_command = TelegramLinkCommand(
            update_id=conflict_update,
            telegram_user_id=after_id,
            telegram_chat_id=after_id,
            telegram_display_name="Conflicting Telegram",
            token=_link_token(conflict_link.url),
        )
        conflict = await links.process_command(command=conflict_command)
        assert conflict.outcome is TelegramLinkOutcome.CONFLICT
        duplicate = await links.process_command(command=conflict_command)
        assert duplicate.outcome is TelegramLinkOutcome.DUPLICATE

        async with pool.acquire() as connection:
            identity_count = await connection.fetchval(
                """
                SELECT COUNT(*)
                FROM external_identities
                WHERE provider = 'telegram'
                    AND external_user_id = ANY($1::text[])
                """,
                [str(before_id), str(after_id)],
            )
        assert identity_count == 2
    finally:
        await _drop_failure_triggers(pool)
        await pool.close()
        if user_ids:
            cleanup = await asyncpg.connect(database_url)
            try:
                await cleanup.execute(
                    "DELETE FROM telegram_update_claims WHERE update_id = ANY($1::bigint[])",
                    telegram_update_ids,
                )
                await cleanup.execute(
                    "DELETE FROM users WHERE id = ANY($1::uuid[])",
                    user_ids,
                )
            finally:
                await cleanup.close()


async def _assert_retryable(
    pool: asyncpg.Pool,
    user_id: UUID,
    update_id: int,
) -> None:
    async with pool.acquire() as connection:
        update_status = await connection.fetchval(
            "SELECT status FROM telegram_update_claims WHERE update_id = $1",
            update_id,
        )
        pending_tokens = await connection.fetchval(
            """
            SELECT COUNT(*)
            FROM telegram_link_tokens
            WHERE user_id = $1
                AND consumed_at IS NULL
            """,
            user_id,
        )
    assert update_status == "received"
    assert pending_tokens == 1


@pytest.mark.skipif(
    DATABASE_URL is None,
    reason="TEST_POSTGRES_DATABASE_URL is not configured",
)
def test_link_transaction_failures_roll_back_and_retry_once() -> None:
    assert DATABASE_URL is not None
    asyncio.run(verify_transactional_link_retry(DATABASE_URL))
