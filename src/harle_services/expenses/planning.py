from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from harle_domain.expenses import (
    ARS_CURRENCY,
    BudgetAllocation,
    BudgetCategory,
    ExpenseCategory,
    ExpenseEntryType,
    ExpenseRepository,
    FinanceBudgetRepository,
    FinanceBudgetSettings,
    FinanceCategoryAnalysis,
    FinanceTrendPoint,
    IncomeHistoryEntry,
    IncomeHistoryRepository,
    MonthlyFinanceAnalysis,
)

CENT = Decimal("0.01")
HUNDRED = Decimal("100")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class FinancePlanningService:
    expenses: ExpenseRepository
    incomes: IncomeHistoryRepository
    budgets: FinanceBudgetRepository
    clock: Callable[[], datetime] = utc_now

    async def save_income(
        self,
        *,
        user_id: UUID,
        amount: Decimal,
        effective_on: date,
        timezone_name: str,
    ) -> IncomeHistoryEntry:
        now = self._now()
        if effective_on > _local_date(now, timezone_name):
            raise ValueError("Income cannot become effective in the future.")
        entry = IncomeHistoryEntry(
            id=uuid4(),
            user_id=user_id,
            amount=amount,
            currency=ARS_CURRENCY,
            effective_on=effective_on,
            created_at=now,
            updated_at=now,
        )
        return await self.incomes.save(user_id=user_id, entry=entry)

    async def income_history(
        self,
        *,
        user_id: UUID,
    ) -> Sequence[IncomeHistoryEntry]:
        return await self.incomes.list(user_id=user_id)

    async def save_budget(
        self,
        *,
        user_id: UUID,
        percentages: Mapping[BudgetCategory, Decimal],
    ) -> FinanceBudgetSettings:
        now = self._now()
        existing = await self.budgets.get(user_id=user_id)
        settings = FinanceBudgetSettings(
            user_id=user_id,
            allocations=tuple(
                BudgetAllocation(category, percentages[category])
                for category in BudgetCategory
            ),
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )
        return await self.budgets.save(user_id=user_id, settings=settings)

    async def get_budget(
        self,
        *,
        user_id: UUID,
    ) -> FinanceBudgetSettings | None:
        return await self.budgets.get(user_id=user_id)

    async def analyze_month(
        self,
        *,
        user_id: UUID,
        year: int,
        month: int,
        timezone_name: str,
    ) -> MonthlyFinanceAnalysis:
        # The analysis keeps named monetary intermediates to preserve its auditability.
        # pylint: disable=too-many-locals
        month_start = date(year, month, 1)
        next_month = (
            date(year + 1, 1, 1)
            if month == 12
            else date(year, month + 1, 1)
        )
        now_date = _local_date(self._now(), timezone_name)
        month_end = next_month - timedelta(days=1)
        cutoff = now_date if month_start <= now_date <= month_end else month_end
        trend_start = cutoff - timedelta(days=41)
        query_start = min(month_start, trend_start)
        all_transactions = await self.expenses.list_for_range(
            user_id=user_id,
            start_date=query_start,
            end_date=cutoff,
        )
        transactions = tuple(
            transaction
            for transaction in all_transactions
            if month_start <= transaction.transaction_date <= cutoff
        )
        income = await self.incomes.get_effective(
            user_id=user_id,
            effective_on_or_before=cutoff,
        )
        budget = await self.budgets.get(user_id=user_id)
        gross_expenses = _sum_by_type(transactions, ExpenseEntryType.EXPENSE)
        gross_refunds = _sum_by_type(transactions, ExpenseEntryType.REFUND)
        net_expenses = gross_expenses - gross_refunds
        remaining = income.amount - net_expenses if income is not None else None
        savings_target = (
            _money(
                income.amount
                * budget.percentage_for(BudgetCategory.SAVINGS)
                / HUNDRED,
            )
            if income is not None and budget is not None
            else None
        )
        return MonthlyFinanceAnalysis(
            year=year,
            month=month,
            income=income,
            budget=budget,
            gross_expenses=gross_expenses,
            gross_refunds=gross_refunds,
            net_expenses=net_expenses,
            remaining_income=remaining,
            savings_target=savings_target,
            categories=_category_analysis(
                transactions,
                income_amount=income.amount if income is not None else None,
                budget=budget,
                remaining_income=remaining,
            ),
            trend=_weekly_trend(all_transactions, trend_start, cutoff),
            transactions=transactions,
        )

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Finance service clock must be timezone-aware.")
        return now


def _sum_by_type(
    transactions: Sequence,
    entry_type: ExpenseEntryType,
) -> Decimal:
    return sum(
        (
            transaction.amount
            for transaction in transactions
            if transaction.entry_type is entry_type
        ),
        Decimal("0.00"),
    )


def _category_analysis(
    transactions: Sequence,
    *,
    income_amount: Decimal | None,
    budget: FinanceBudgetSettings | None,
    remaining_income: Decimal | None,
) -> Sequence[FinanceCategoryAnalysis]:
    net_by_category = {category: Decimal("0.00") for category in ExpenseCategory}
    for transaction in transactions:
        contribution = transaction.amount
        if transaction.entry_type is ExpenseEntryType.REFUND:
            contribution = -contribution
        net_by_category[transaction.category] += contribution

    results: list[FinanceCategoryAnalysis] = []
    for category in BudgetCategory:
        actual = (
            remaining_income
            if category is BudgetCategory.SAVINGS
            else net_by_category[ExpenseCategory(category.value)]
        )
        budget_percentage = (
            budget.percentage_for(category) if budget is not None else None
        )
        budget_amount = (
            _money(income_amount * budget_percentage / HUNDRED)
            if income_amount is not None and budget_percentage is not None
            else None
        )
        results.append(
            FinanceCategoryAnalysis(
                category=category,
                actual_amount=actual,
                income_percentage=_percentage(actual, income_amount),
                budget_percentage=budget_percentage,
                budget_amount=budget_amount,
                budget_used_percentage=_percentage(actual, budget_amount),
            ),
        )
    return tuple(results)


def _weekly_trend(
    transactions: Sequence,
    start_date: date,
    end_date: date,
) -> Sequence[FinanceTrendPoint]:
    first_week = start_date - timedelta(days=start_date.weekday())
    totals: dict[date, Decimal] = {}
    current = first_week
    while current <= end_date:
        totals[current] = Decimal("0.00")
        current += timedelta(days=7)
    for transaction in transactions:
        if not start_date <= transaction.transaction_date <= end_date:
            continue
        week = transaction.transaction_date - timedelta(
            days=transaction.transaction_date.weekday(),
        )
        contribution = transaction.amount
        if transaction.entry_type is ExpenseEntryType.REFUND:
            contribution = -contribution
        totals[week] += contribution
    return tuple(
        FinanceTrendPoint(week_starts_on=week, net_spent=amount)
        for week, amount in sorted(totals.items())
    )


def _percentage(
    value: Decimal | None,
    base: Decimal | None,
) -> Decimal | None:
    if value is None or base is None or base == 0:
        return None
    return (value * HUNDRED / base).quantize(CENT, rounding=ROUND_HALF_UP)


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _local_date(now: datetime, timezone_name: str) -> date:
    try:
        local_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {timezone_name}.") from exc
    return now.astimezone(local_timezone).date()
