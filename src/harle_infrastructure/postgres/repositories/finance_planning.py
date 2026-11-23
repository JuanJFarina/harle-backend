from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import asyncpg

from harle_domain.expenses import (
    BudgetAllocation,
    BudgetCategory,
    FinanceBudgetSettings,
    IncomeHistoryEntry,
)

INCOME_COLUMNS = """
    id, user_id, amount, currency, effective_on, created_at, updated_at
"""

BUDGET_PERCENTAGE_COLUMNS = {
    BudgetCategory.RENT: "rent_percentage",
    BudgetCategory.ESSENTIAL_SERVICES: "essential_services_percentage",
    BudgetCategory.NON_ESSENTIAL_SERVICES: "non_essential_services_percentage",
    BudgetCategory.HOME: "home_percentage",
    BudgetCategory.TRANSPORT: "transport_percentage",
    BudgetCategory.OUTINGS: "outings_percentage",
    BudgetCategory.SHOPPING: "shopping_percentage",
    BudgetCategory.OTHER: "other_percentage",
    BudgetCategory.SAVINGS: "savings_percentage",
}


@dataclass(frozen=True, slots=True)
class PostgresIncomeHistoryRepository:
    pool: asyncpg.Pool

    async def get_effective(
        self,
        *,
        user_id: UUID,
        effective_on_or_before: date,
    ) -> IncomeHistoryEntry | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT {INCOME_COLUMNS}
                FROM income_history
                WHERE user_id = $1 AND effective_on <= $2
                ORDER BY effective_on DESC
                LIMIT 1
                """,
                user_id,
                effective_on_or_before,
            )
        return _income(row) if row is not None else None

    async def list(self, *, user_id: UUID) -> list[IncomeHistoryEntry]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT {INCOME_COLUMNS}
                FROM income_history
                WHERE user_id = $1
                ORDER BY effective_on DESC
                """,
                user_id,
            )
        return [_income(row) for row in rows]

    async def save(
        self,
        *,
        user_id: UUID,
        entry: IncomeHistoryEntry,
    ) -> IncomeHistoryEntry:
        if entry.user_id != user_id:
            raise ValueError("Income owner does not match user identifier.")
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                INSERT INTO income_history (
                    id, user_id, amount, currency, effective_on, created_at, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (user_id, effective_on) DO UPDATE
                SET amount = EXCLUDED.amount,
                    currency = EXCLUDED.currency,
                    updated_at = EXCLUDED.updated_at
                RETURNING {INCOME_COLUMNS}
                """,
                entry.id,
                user_id,
                entry.amount,
                entry.currency,
                entry.effective_on,
                entry.created_at,
                entry.updated_at,
            )
        if row is None:
            raise RuntimeError("Could not save income history.")
        return _income(row)


@dataclass(frozen=True, slots=True)
class PostgresFinanceBudgetRepository:
    pool: asyncpg.Pool

    async def get(self, *, user_id: UUID) -> FinanceBudgetSettings | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT *
                FROM finance_budget_settings
                WHERE user_id = $1
                """,
                user_id,
            )
        return _budget(row) if row is not None else None

    async def save(
        self,
        *,
        user_id: UUID,
        settings: FinanceBudgetSettings,
    ) -> FinanceBudgetSettings:
        if settings.user_id != user_id:
            raise ValueError("Budget owner does not match user identifier.")
        values = [
            settings.percentage_for(category)
            for category in BudgetCategory
        ]
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO finance_budget_settings (
                    user_id,
                    rent_percentage,
                    essential_services_percentage,
                    non_essential_services_percentage,
                    home_percentage,
                    transport_percentage,
                    outings_percentage,
                    shopping_percentage,
                    other_percentage,
                    savings_percentage,
                    created_at,
                    updated_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12
                )
                ON CONFLICT (user_id) DO UPDATE
                SET rent_percentage = EXCLUDED.rent_percentage,
                    essential_services_percentage =
                        EXCLUDED.essential_services_percentage,
                    non_essential_services_percentage =
                        EXCLUDED.non_essential_services_percentage,
                    home_percentage = EXCLUDED.home_percentage,
                    transport_percentage = EXCLUDED.transport_percentage,
                    outings_percentage = EXCLUDED.outings_percentage,
                    shopping_percentage = EXCLUDED.shopping_percentage,
                    other_percentage = EXCLUDED.other_percentage,
                    savings_percentage = EXCLUDED.savings_percentage,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                user_id,
                *values,
                settings.created_at,
                settings.updated_at,
            )
        if row is None:
            raise RuntimeError("Could not save finance budget.")
        return _budget(row)


def _income(row: asyncpg.Record) -> IncomeHistoryEntry:
    return IncomeHistoryEntry(
        id=_uuid(row, "id"),
        user_id=_uuid(row, "user_id"),
        amount=_decimal(row, "amount"),
        currency=_text(row, "currency"),
        effective_on=_date(row, "effective_on"),
        created_at=_datetime(row, "created_at"),
        updated_at=_datetime(row, "updated_at"),
    )


def _budget(row: asyncpg.Record) -> FinanceBudgetSettings:
    return FinanceBudgetSettings(
        user_id=_uuid(row, "user_id"),
        allocations=tuple(
            BudgetAllocation(category, _decimal(row, column))
            for category, column in BUDGET_PERCENTAGE_COLUMNS.items()
        ),
        created_at=_datetime(row, "created_at"),
        updated_at=_datetime(row, "updated_at"),
    )


def _uuid(row: asyncpg.Record, key: str) -> UUID:
    value: object = row[key]
    if not isinstance(value, UUID):
        raise TypeError(f"Expected {key} to be a UUID.")
    return value


def _decimal(row: asyncpg.Record, key: str) -> Decimal:
    value: object = row[key]
    if not isinstance(value, Decimal):
        raise TypeError(f"Expected {key} to be numeric.")
    return value


def _text(row: asyncpg.Record, key: str) -> str:
    value: object = row[key]
    if not isinstance(value, str):
        raise TypeError(f"Expected {key} to be text.")
    return value


def _date(row: asyncpg.Record, key: str) -> date:
    value: object = row[key]
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"Expected {key} to be a date.")
    return value


def _datetime(row: asyncpg.Record, key: str) -> datetime:
    value: object = row[key]
    if not isinstance(value, datetime):
        raise TypeError(f"Expected {key} to be a datetime.")
    return value
