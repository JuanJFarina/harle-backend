import asyncio
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from harle_domain.messaging import TelegramUpdateState
from harle_domain.tools import (
    InternalToolCallInteraction,
    ToolCall,
    ToolCallResult,
)
from harle_infrastructure.postgres import (
    PostgresConversationRepository,
    PostgresConversationStore,
    PostgresTelegramUpdateRepository,
    create_postgres_pool,
    validate_postgres_schema,
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


async def verify_persistent_deduplication(database_url: str) -> None:
    user_id = uuid4()
    update_id = user_id.int % 8_000_000_000 + 1
    second_update_id = update_id + 10
    telegram_user_id = update_id + 1
    chat_id = update_id + 2
    connection = await asyncpg.connect(database_url)
    try:
        for schema_path in SCHEMA_PATHS:
            await connection.execute(schema_path.read_text(encoding="utf-8"))
        await connection.execute(
            """
            INSERT INTO users (
                id, name, display_name, plan_code,
                subscription_status, subscription_synced_at,
                subscription_period_starts_at,
                subscription_period_ends_at
            )
            VALUES (
                $1, 'Dedup User', 'Dedup User', 'free', 'active', NOW(),
                NOW() - INTERVAL '1 day',
                NOW() + INTERVAL '29 days'
            )
            """,
            user_id,
        )
    finally:
        await connection.close()

    pool = await create_postgres_pool(
        database_url=database_url,
        min_size=1,
        max_size=5,
    )
    try:
        updates = PostgresTelegramUpdateRepository(pool)
        results = await asyncio.gather(
            *(
                updates.receive(
                    update_id=update_id,
                    telegram_user_id=telegram_user_id,
                    telegram_chat_id=chat_id,
                    message_text="hello",
                )
                for _ in range(5)
            ),
        )
        assert all(result.state is TelegramUpdateState.RECEIVED for result in results)
        assert sum(result.newly_persisted for result in results) == 1
        await updates.mark_processing([update_id])
    finally:
        await pool.close()

    restarted_pool = await create_postgres_pool(
        database_url=database_url,
        min_size=1,
        max_size=2,
    )
    try:
        await validate_postgres_schema(restarted_pool)
        updates = PostgresTelegramUpdateRepository(restarted_pool)
        receipt = await updates.receive(
            update_id=update_id,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=chat_id,
            message_text="hello",
        )
        assert receipt.state is TelegramUpdateState.PROCESSING
        assert not receipt.newly_persisted
        second_receipt = await updates.receive(
            update_id=second_update_id,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=chat_id,
            message_text="again",
        )
        assert second_receipt.newly_persisted

        conversations = PostgresConversationStore(
            repository=PostgresConversationRepository(restarted_pool),
            user_id=user_id,
            telegram_chat_id=chat_id,
        )
        interaction = InternalToolCallInteraction(
            tool_calls=[ToolCall(tool_name="list_events", tool_args={})],
            tool_results=[
                ToolCallResult(called_tool_name="list_events", result={"ok": True}),
            ],
        )
        await conversations.save_tool_call(
            interaction=interaction,
            interaction_index=0,
            model="fake",
        )
        await conversations.save(
            prompt="hello\nagain",
            response_text="hi",
            model="fake",
            telegram_update_ids=[update_id, second_update_id],
        )

        async with restarted_pool.acquire() as check:
            rows = await check.fetch(
                """
                SELECT
                    update_id,
                    status,
                    conversation_id
                FROM telegram_update_claims
                WHERE update_id = ANY($1::bigint[])
                ORDER BY update_id
                """,
                [update_id, second_update_id],
            )
        assert [row["status"] for row in rows] == ["delivered", "delivered"]
        assert len({row["conversation_id"] for row in rows}) == 1
    finally:
        await restarted_pool.close()
        cleanup = await asyncpg.connect(database_url)
        try:
            await cleanup.execute(
                "DELETE FROM telegram_update_claims WHERE update_id = ANY($1::bigint[])",
                [update_id, second_update_id],
            )
            await cleanup.execute("DELETE FROM users WHERE id = $1", user_id)
        finally:
            await cleanup.close()


@pytest.mark.skipif(
    DATABASE_URL is None,
    reason="TEST_POSTGRES_DATABASE_URL is not configured",
)
def test_postgres_update_deduplication_survives_restart() -> None:
    assert DATABASE_URL is not None
    asyncio.run(verify_persistent_deduplication(DATABASE_URL))
