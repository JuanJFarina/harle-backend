import asyncio
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

from harle_domain.expenses import ExpenseCategory
from harle_infrastructure.postgres import (
    PostgresExpenseRepository,
    create_postgres_pool,
)
from harle_services.expenses import (
    CreateExpense,
    CreateInstallmentExpense,
    ExpenseChanges,
    ExpenseService,
)

DATABASE_URL = os.environ.get("TEST_POSTGRES_DATABASE_URL")
ROOT = Path(__file__).parents[2]
SCHEMA_PATHS = (
    ROOT / "scripts" / "apply_multi_user_runtime.sql",
    ROOT / "scripts" / "apply_subscription_interactions.sql",
    ROOT / "scripts" / "apply_interaction_frequency.sql",
    ROOT / "scripts" / "apply_internal_expenses.sql",
)
NOW = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)


async def _insert_user(
    connection: asyncpg.Connection,
    user_id: UUID,
    name: str,
) -> None:
    await connection.execute(
        """
        INSERT INTO users (
            id, name, display_name, plan_code,
            subscription_status, subscription_synced_at,
            subscription_period_starts_at,
            subscription_period_ends_at
        )
        VALUES (
            $1, $2, $2, 'free', 'active', NOW(),
            NOW() - INTERVAL '1 day',
            NOW() + INTERVAL '29 days'
        )
        """,
        user_id,
        name,
    )


async def verify_expense_isolation(database_url: str) -> None:
    first_id = uuid4()
    second_id = uuid4()
    connection = await asyncpg.connect(database_url)
    try:
        for schema_path in SCHEMA_PATHS:
            await connection.execute(schema_path.read_text(encoding="utf-8"))
        await _insert_user(connection, first_id, "First Expense User")
        await _insert_user(connection, second_id, "Second Expense User")
    finally:
        await connection.close()

    pool = await create_postgres_pool(
        database_url=database_url,
        min_size=1,
        max_size=2,
    )
    try:
        service = ExpenseService(
            PostgresExpenseRepository(pool),
            clock=lambda: NOW,
        )
        first = await service.add_installments(
            user_id=first_id,
            timezone_name="UTC",
            purchase=CreateInstallmentExpense(
                expense=CreateExpense(
                    amount=Decimal("100.00"),
                    category=ExpenseCategory.HOME,
                    description="First purchase",
                    transaction_date=date(2026, 8, 20),
                ),
                installment_count=3,
            ),
        )
        await service.add_expense(
            user_id=second_id,
            timezone_name="UTC",
            expense=CreateExpense(
                amount=Decimal("50.00"),
                category=ExpenseCategory.TRANSPORT,
                description="Second expense",
                transaction_date=date(2026, 8, 20),
            ),
        )

        first_day = await service.list_for_date(
            user_id=first_id,
            transaction_date=date(2026, 8, 20),
        )
        second_day = await service.list_for_date(
            user_id=second_id,
            transaction_date=date(2026, 8, 20),
        )
        assert {transaction.user_id for transaction in first_day} == {first_id}
        assert {transaction.user_id for transaction in second_day} == {second_id}

        target_id = first.transactions[1].id
        denied = await service.update(
            user_id=second_id,
            timezone_name="UTC",
            transaction_id=target_id,
            changes=ExpenseChanges(amount=Decimal("999.00")),
        )
        assert not denied

        updated = await service.update(
            user_id=first_id,
            timezone_name="UTC",
            transaction_id=target_id,
            changes=ExpenseChanges(amount=Decimal("120.00")),
        )
        assert len(updated) == 3
        assert sum(
            (transaction.amount for transaction in updated),
            Decimal("0.00"),
        ) == Decimal("120.00")

        deleted = await service.delete(
            user_id=first_id,
            transaction_id=target_id,
        )
        assert len(deleted) == 3
        assert not await service.list_for_date(
            user_id=first_id,
            transaction_date=date(2026, 8, 20),
        )
        assert await service.list_for_date(
            user_id=second_id,
            transaction_date=date(2026, 8, 20),
        )
    finally:
        await pool.close()
        cleanup = await asyncpg.connect(database_url)
        try:
            await cleanup.execute(
                "DELETE FROM users WHERE id = ANY($1::uuid[])",
                [first_id, second_id],
            )
        finally:
            await cleanup.close()


@pytest.mark.skipif(
    DATABASE_URL is None,
    reason="TEST_POSTGRES_DATABASE_URL is not configured",
)
def test_postgres_expense_isolation_and_installment_lifecycle() -> None:
    assert DATABASE_URL is not None
    asyncio.run(verify_expense_isolation(DATABASE_URL))
