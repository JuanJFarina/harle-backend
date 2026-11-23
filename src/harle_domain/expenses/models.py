from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

ARS_CURRENCY = "ARS"
CENT = Decimal("0.01")


class ExpenseEntryType(str, Enum):
    EXPENSE = "expense"
    REFUND = "refund"


class ExpenseCategory(str, Enum):
    RENT = "rent"
    ESSENTIAL_SERVICES = "essential_services"
    NON_ESSENTIAL_SERVICES = "non_essential_services"
    HOME = "home"
    TRANSPORT = "transport"
    OUTINGS = "outings"
    SHOPPING = "shopping"
    OTHER = "other"


class BudgetCategory(str, Enum):
    RENT = "rent"
    ESSENTIAL_SERVICES = "essential_services"
    NON_ESSENTIAL_SERVICES = "non_essential_services"
    HOME = "home"
    TRANSPORT = "transport"
    OUTINGS = "outings"
    SHOPPING = "shopping"
    OTHER = "other"
    SAVINGS = "savings"


@dataclass(frozen=True, slots=True)
class IncomeHistoryEntry:
    id: UUID
    user_id: UUID
    amount: Decimal
    currency: str
    effective_on: date
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _validate_amount(self.amount)
        if self.currency != ARS_CURRENCY:
            raise ValueError("Income history supports only ARS.")


@dataclass(frozen=True, slots=True)
class BudgetAllocation:
    category: BudgetCategory
    percentage: Decimal

    def __post_init__(self) -> None:
        if (
            not self.percentage.is_finite()
            or self.percentage < 0
            or self.percentage > 100
            or self.percentage.quantize(CENT) != self.percentage
        ):
            raise ValueError("Budget percentages must be between 0 and 100.")


@dataclass(frozen=True, slots=True)
class FinanceBudgetSettings:
    user_id: UUID
    allocations: Sequence[BudgetAllocation]
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        categories = [allocation.category for allocation in self.allocations]
        if len(categories) != len(set(categories)):
            raise ValueError("Budget categories must be unique.")
        if set(categories) != set(BudgetCategory):
            raise ValueError("Every budget category must have an allocation.")
        total = sum(
            (allocation.percentage for allocation in self.allocations),
            Decimal("0.00"),
        )
        if total != Decimal("100.00"):
            raise ValueError("Budget percentages must total exactly 100.")

    def percentage_for(self, category: BudgetCategory) -> Decimal:
        for allocation in self.allocations:
            if allocation.category is category:
                return allocation.percentage
        raise ValueError(f"Missing budget allocation for {category.value}.")


@dataclass(frozen=True, slots=True)
class ExpenseDetails:
    entry_type: ExpenseEntryType
    amount: Decimal
    currency: str
    category: ExpenseCategory
    transaction_date: date
    description: str

    def __post_init__(self) -> None:
        _validate_amount(self.amount)
        if self.currency != ARS_CURRENCY:
            raise ValueError("Internal expenses support only ARS.")
        if not self.description.strip():
            raise ValueError("Expense description cannot be empty.")


@dataclass(frozen=True, slots=True)
class ExpenseInstallment:
    group_id: UUID
    number: int
    count: int

    def __post_init__(self) -> None:
        if not 2 <= self.count <= 12:
            raise ValueError("Installment count must be between 2 and 12.")
        if not 1 <= self.number <= self.count:
            raise ValueError("Installment number must be within the installment count.")


@dataclass(frozen=True, slots=True)
class ExpenseTimestamps:
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ExpenseTransaction:
    id: UUID
    user_id: UUID
    details: ExpenseDetails
    installment: ExpenseInstallment | None
    timestamps: ExpenseTimestamps

    def __post_init__(self) -> None:
        _validate_installments(self)

    @property
    def is_installment(self) -> bool:
        return self.installment is not None

    @property
    def entry_type(self) -> ExpenseEntryType:
        return self.details.entry_type

    @property
    def amount(self) -> Decimal:
        return self.details.amount

    @property
    def currency(self) -> str:
        return self.details.currency

    @property
    def category(self) -> ExpenseCategory:
        return self.details.category

    @property
    def transaction_date(self) -> date:
        return self.details.transaction_date

    @property
    def description(self) -> str:
        return self.details.description

    @property
    def installment_group_id(self) -> UUID | None:
        return self.installment.group_id if self.installment is not None else None

    @property
    def installment_number(self) -> int | None:
        return self.installment.number if self.installment is not None else None

    @property
    def installment_count(self) -> int | None:
        return self.installment.count if self.installment is not None else None

    @property
    def created_at(self) -> datetime:
        return self.timestamps.created_at

    @property
    def updated_at(self) -> datetime:
        return self.timestamps.updated_at


@dataclass(frozen=True, slots=True)
class ExpenseCategoryTotal:
    category: ExpenseCategory
    amount: Decimal


@dataclass(frozen=True, slots=True)
class ExpenseSummary:
    year: int
    month: int
    category_totals: Sequence[ExpenseCategoryTotal]
    total: Decimal


@dataclass(frozen=True, slots=True)
class FinanceCategoryAnalysis:
    category: BudgetCategory
    actual_amount: Decimal
    income_percentage: Decimal | None
    budget_percentage: Decimal | None
    budget_amount: Decimal | None
    budget_used_percentage: Decimal | None


@dataclass(frozen=True, slots=True)
class FinanceTrendPoint:
    week_starts_on: date
    net_spent: Decimal


@dataclass(frozen=True, slots=True)
class MonthlyFinanceAnalysis:
    year: int
    month: int
    income: IncomeHistoryEntry | None
    budget: FinanceBudgetSettings | None
    gross_expenses: Decimal
    gross_refunds: Decimal
    net_expenses: Decimal
    remaining_income: Decimal | None
    savings_target: Decimal | None
    categories: Sequence[FinanceCategoryAnalysis]
    trend: Sequence[FinanceTrendPoint]
    transactions: Sequence[ExpenseTransaction]


def _validate_amount(amount: Decimal) -> None:
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Expense amount must be a positive finite value.")
    if amount.quantize(CENT) != amount:
        raise ValueError("Expense amount cannot have more than two decimal places.")


def _validate_installments(transaction: ExpenseTransaction) -> None:
    if transaction.installment is None:
        return
    if transaction.entry_type is ExpenseEntryType.REFUND:
        raise ValueError("Refunds cannot be split into installments.")
