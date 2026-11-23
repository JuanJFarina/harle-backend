import asyncio
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

from harle_domain.expenses import BudgetCategory
from harle_infrastructure.postgres import (
    PostgresExpenseRepository,
    PostgresFinanceBudgetRepository,
    PostgresIncomeHistoryRepository,
    create_postgres_pool,
)
from harle_services.expenses import FinancePlanningService

DATABASE_URL = os.environ.get("TEST_POSTGRES_DATABASE_URL")
ROOT = Path(__file__).parents[2]
SCHEMA_PATHS = (
    ROOT / "scripts" / "apply_multi_user_runtime.sql",
    ROOT / "scripts" / "apply_subscription_interactions.sql",
    ROOT / "scripts" / "apply_interaction_frequency.sql",
    ROOT / "scripts" / "apply_internal_expenses.sql",
    ROOT / "scripts" / "apply_finance_planning.sql",
)
NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)


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
            NOW() - INTERVAL '1 day', NOW() + INTERVAL '29 days'
        )
        """,
        user_id,
        name,
    )


async def verify_finance_planning(database_url: str) -> None:
    first_id = uuid4()
    second_id = uuid4()
    connection = await asyncpg.connect(database_url)
    try:
        for schema_path in SCHEMA_PATHS:
            await connection.execute(schema_path.read_text(encoding="utf-8"))
        await _insert_user(connection, first_id, "First Finance User")
        await _insert_user(connection, second_id, "Second Finance User")
    finally:
        await connection.close()

    pool = await create_postgres_pool(
        database_url=database_url,
        min_size=1,
        max_size=2,
    )
    try:
        service = FinancePlanningService(
            expenses=PostgresExpenseRepository(pool),
            incomes=PostgresIncomeHistoryRepository(pool),
            budgets=PostgresFinanceBudgetRepository(pool),
            clock=lambda: NOW,
        )
        await service.save_income(
            user_id=first_id,
            amount=Decimal("1000.00"),
            effective_on=date(2026, 10, 1),
        )
        await service.save_income(
            user_id=first_id,
            amount=Decimal("1200.00"),
            effective_on=date(2026, 11, 1),
        )
        percentages = {category: Decimal("0.00") for category in BudgetCategory}
        percentages[BudgetCategory.HOME] = Decimal("70.00")
        percentages[BudgetCategory.SAVINGS] = Decimal("30.00")
        await service.save_budget(user_id=first_id, percentages=percentages)

        october = await service.analyze_month(
            user_id=first_id,
            year=2026,
            month=10,
        )
        second = await service.analyze_month(
            user_id=second_id,
            year=2026,
            month=10,
        )

        assert october.income is not None
        assert october.income.amount == Decimal("1000.00")
        assert october.budget is not None
        assert second.income is None
        assert second.budget is None
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
def test_postgres_finance_planning_is_isolated_and_effective_dated() -> None:
    assert DATABASE_URL is not None
    asyncio.run(verify_finance_planning(DATABASE_URL))
