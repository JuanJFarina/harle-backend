import asyncio
from collections.abc import Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from harle_domain.expenses import (
    BudgetCategory,
    ExpenseCategory,
    ExpenseEntryType,
    ExpenseTransaction,
    FinanceBudgetSettings,
    IncomeHistoryEntry,
)
from harle_services.expenses import (
    CreateExpense,
    ExpenseService,
    FinancePlanningService,
)

NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)


class FakeExpenses:
    def __init__(self) -> None:
        self.transactions: list[ExpenseTransaction] = []
        self.requested_ranges: list[tuple[date, date]] = []

    async def create(
        self,
        *,
        user_id: UUID,
        transactions: Sequence[ExpenseTransaction],
    ) -> Sequence[ExpenseTransaction]:
        assert all(item.user_id == user_id for item in transactions)
        self.transactions.extend(transactions)
        return transactions

    async def list_for_range(
        self,
        *,
        user_id: UUID,
        start_date: date,
        end_date: date,
    ) -> Sequence[ExpenseTransaction]:
        self.requested_ranges.append((start_date, end_date))
        return [
            item
            for item in self.transactions
            if item.user_id == user_id
            and start_date <= item.transaction_date <= end_date
        ]


class FakeIncomes:
    def __init__(self) -> None:
        self.entries: dict[tuple[UUID, date], IncomeHistoryEntry] = {}

    async def get_effective(
        self,
        *,
        user_id: UUID,
        effective_on_or_before: date,
    ) -> IncomeHistoryEntry | None:
        eligible = [
            item
            for item in self.entries.values()
            if item.user_id == user_id
            and item.effective_on <= effective_on_or_before
        ]
        return max(eligible, key=lambda item: item.effective_on, default=None)

    async def list(self, *, user_id: UUID) -> Sequence[IncomeHistoryEntry]:
        return sorted(
            (item for item in self.entries.values() if item.user_id == user_id),
            key=lambda item: item.effective_on,
            reverse=True,
        )

    async def save(
        self,
        *,
        user_id: UUID,
        entry: IncomeHistoryEntry,
    ) -> IncomeHistoryEntry:
        self.entries[user_id, entry.effective_on] = entry
        return entry


class FakeBudgets:
    def __init__(self) -> None:
        self.settings: dict[UUID, FinanceBudgetSettings] = {}

    async def get(self, *, user_id: UUID) -> FinanceBudgetSettings | None:
        return self.settings.get(user_id)

    async def save(
        self,
        *,
        user_id: UUID,
        settings: FinanceBudgetSettings,
    ) -> FinanceBudgetSettings:
        self.settings[user_id] = settings
        return settings


def test_income_history_uses_latest_entry_applicable_to_the_month() -> None:
    async def verify() -> None:
        service, _, user_id = _service()
        await service.save_income(
            user_id=user_id,
            amount=Decimal("1000.00"),
            effective_on=date(2026, 10, 1),
            timezone_name="UTC",
        )
        await service.save_income(
            user_id=user_id,
            amount=Decimal("1200.00"),
            effective_on=date(2026, 11, 1),
            timezone_name="UTC",
        )

        october = await service.analyze_month(
            user_id=user_id,
            year=2026,
            month=10,
            timezone_name="UTC",
        )
        november = await service.analyze_month(
            user_id=user_id,
            year=2026,
            month=11,
            timezone_name="UTC",
        )

        assert october.income is not None
        assert october.income.amount == Decimal("1000.00")
        assert november.income is not None
        assert november.income.amount == Decimal("1200.00")

    asyncio.run(verify())


def test_analysis_nets_refunds_and_compares_current_budget_to_income() -> None:
    async def verify() -> None:
        service, expense_repository, user_id = _service()
        expenses = ExpenseService(expense_repository, clock=lambda: NOW)
        await service.save_income(
            user_id=user_id,
            amount=Decimal("1000.00"),
            effective_on=date(2026, 10, 1),
            timezone_name="UTC",
        )
        percentages = {category: Decimal("0.00") for category in BudgetCategory}
        percentages[BudgetCategory.HOME] = Decimal("80.00")
        percentages[BudgetCategory.SAVINGS] = Decimal("20.00")
        await service.save_budget(user_id=user_id, percentages=percentages)
        for amount, entry_type in (
            (Decimal("100.00"), ExpenseEntryType.EXPENSE),
            (Decimal("20.00"), ExpenseEntryType.REFUND),
        ):
            await expenses.add_expense(
                user_id=user_id,
                timezone_name="UTC",
                expense=CreateExpense(
                    amount=amount,
                    category=ExpenseCategory.HOME,
                    description="Home",
                    transaction_date=date(2026, 11, 10),
                    entry_type=entry_type,
                ),
            )

        analysis = await service.analyze_month(
            user_id=user_id,
            year=2026,
            month=11,
            timezone_name="UTC",
        )
        home = next(
            item
            for item in analysis.categories
            if item.category is BudgetCategory.HOME
        )

        assert analysis.gross_expenses == Decimal("100.00")
        assert analysis.gross_refunds == Decimal("20.00")
        assert analysis.net_expenses == Decimal("80.00")
        assert analysis.remaining_income == Decimal("920.00")
        assert analysis.savings_target == Decimal("200.00")
        assert home.actual_amount == Decimal("80.00")
        assert home.income_percentage == Decimal("8.00")
        savings = next(
            item
            for item in analysis.categories
            if item.category is BudgetCategory.SAVINGS
        )
        assert savings.actual_amount == Decimal("920.00")
        assert savings.budget_amount == Decimal("200.00")
        assert savings.budget_used_percentage == Decimal("460.00")

    asyncio.run(verify())


def test_missing_income_and_budget_are_explicit() -> None:
    async def verify() -> None:
        service, _, user_id = _service()

        analysis = await service.analyze_month(
            user_id=user_id,
            year=2026,
            month=11,
            timezone_name="UTC",
        )

        assert analysis.income is None
        assert analysis.budget is None
        assert analysis.remaining_income is None
        assert analysis.savings_target is None
        assert all(item.budget_amount is None for item in analysis.categories)
        savings = next(
            item
            for item in analysis.categories
            if item.category is BudgetCategory.SAVINGS
        )
        assert savings.actual_amount is None

    asyncio.run(verify())


def test_current_month_cutoff_uses_user_local_date() -> None:
    async def verify() -> None:
        expenses = FakeExpenses()
        service = FinancePlanningService(
            expenses=expenses,  # type: ignore[arg-type]
            incomes=FakeIncomes(),
            budgets=FakeBudgets(),
            clock=lambda: datetime(2026, 11, 24, 1, tzinfo=timezone.utc),
        )
        user_id = uuid4()
        expense_service = ExpenseService(
            expenses,  # type: ignore[arg-type]
            clock=lambda: datetime(2026, 11, 24, 12, tzinfo=timezone.utc),
        )
        await expense_service.add_expense(
            user_id=user_id,
            timezone_name="UTC",
            expense=CreateExpense(
                amount=Decimal("100.00"),
                category=ExpenseCategory.HOME,
                description="Tomorrow locally",
                transaction_date=date(2026, 11, 24),
            ),
        )

        analysis = await service.analyze_month(
            user_id=user_id,
            year=2026,
            month=11,
            timezone_name="America/Argentina/Cordoba",
        )

        assert analysis.net_expenses == Decimal("0.00")
        assert not analysis.transactions

    asyncio.run(verify())


def test_trend_uses_the_requested_rolling_period() -> None:
    async def verify() -> None:
        service, expenses, user_id = _service()

        thirty_days = await service.analyze_month(
            user_id=user_id,
            year=2026,
            month=11,
            timezone_name="UTC",
            trend_days=30,
        )
        ninety_days = await service.analyze_month(
            user_id=user_id,
            year=2026,
            month=11,
            timezone_name="UTC",
            trend_days=90,
        )
        year = await service.analyze_month(
            user_id=user_id,
            year=2026,
            month=11,
            timezone_name="UTC",
            trend_days=365,
        )

        assert expenses.requested_ranges == [
            (date(2026, 10, 25), date(2026, 11, 23)),
            (date(2026, 8, 26), date(2026, 11, 23)),
            (date(2025, 11, 24), date(2026, 11, 23)),
        ]
        assert len(thirty_days.trend) == 6
        assert len(ninety_days.trend) == 14
        assert len(year.trend) == 53

    asyncio.run(verify())


def test_income_effective_date_uses_user_local_today() -> None:
    async def verify() -> None:
        service = FinancePlanningService(
            expenses=FakeExpenses(),  # type: ignore[arg-type]
            incomes=FakeIncomes(),
            budgets=FakeBudgets(),
            clock=lambda: datetime(2026, 11, 23, 12, tzinfo=timezone.utc),
        )

        entry = await service.save_income(
            user_id=uuid4(),
            amount=Decimal("1000.00"),
            effective_on=date(2026, 11, 24),
            timezone_name="Pacific/Kiritimati",
        )

        assert entry.effective_on == date(2026, 11, 24)

    asyncio.run(verify())


def _service() -> tuple[FinancePlanningService, FakeExpenses, UUID]:
    expenses = FakeExpenses()
    return (
        FinancePlanningService(
            expenses=expenses,  # type: ignore[arg-type]
            incomes=FakeIncomes(),
            budgets=FakeBudgets(),
            clock=lambda: NOW,
        ),
        expenses,
        uuid4(),
    )
