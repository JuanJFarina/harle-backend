from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harle_domain.expenses import (
    BudgetCategory,
    ExpenseCategory,
    ExpenseEntryType,
)
from harle_services.expenses import ExpenseChanges

Money = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)]
Percentage = Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)]


class CreateExpensePayload(BaseModel):
    amount: Money
    entry_type: ExpenseEntryType = ExpenseEntryType.EXPENSE
    category: ExpenseCategory
    transaction_date: date | None = None
    description: str = Field(min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid")


class CreateInstallmentExpensePayload(BaseModel):
    total_amount: Money
    category: ExpenseCategory
    transaction_date: date | None = None
    description: str = Field(min_length=1, max_length=500)
    installment_count: int = Field(ge=2, le=12)

    model_config = ConfigDict(extra="forbid")


class UpdateExpensePayload(BaseModel):
    amount: Money | None = None
    entry_type: ExpenseEntryType | None = None
    category: ExpenseCategory | None = None
    transaction_date: date | None = None
    description: str | None = Field(default=None, min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def require_change(self) -> "UpdateExpensePayload":
        self.to_changes()
        return self

    def to_changes(self) -> ExpenseChanges:
        return ExpenseChanges(
            amount=self.amount,
            entry_type=self.entry_type,
            category=self.category,
            transaction_date=self.transaction_date,
            description=self.description,
        )


class SaveIncomePayload(BaseModel):
    amount: Money
    effective_on: date

    model_config = ConfigDict(extra="forbid")


class SaveBudgetPayload(BaseModel):
    rent: Percentage
    essential_services: Percentage
    non_essential_services: Percentage
    home: Percentage
    transport: Percentage
    outings: Percentage
    shopping: Percentage
    other: Percentage
    savings: Percentage

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def require_full_allocation(self) -> "SaveBudgetPayload":
        if sum(self.percentages().values(), Decimal("0.00")) != Decimal("100.00"):
            raise ValueError("Budget percentages must total exactly 100.")
        return self

    def percentages(self) -> dict[BudgetCategory, Decimal]:
        return {
            category: getattr(self, category.value)
            for category in BudgetCategory
        }


class ExpenseTransactionPayload(BaseModel):
    transaction_id: UUID
    entry_type: ExpenseEntryType
    amount: Decimal
    currency: str
    category: ExpenseCategory
    transaction_date: date
    description: str
    installment_group_id: UUID | None
    installment_number: int | None
    installment_count: int | None


class IncomePayload(BaseModel):
    income_id: UUID
    amount: Decimal
    currency: str
    effective_on: date
    created_at: datetime
    updated_at: datetime


class BudgetAllocationPayload(BaseModel):
    category: BudgetCategory
    percentage: Decimal


class BudgetPayload(BaseModel):
    allocations: list[BudgetAllocationPayload]
    created_at: datetime
    updated_at: datetime


class FinanceCategoryPayload(BaseModel):
    category: BudgetCategory
    actual_amount: Decimal
    income_percentage: Decimal | None
    budget_percentage: Decimal | None
    budget_amount: Decimal | None
    budget_used_percentage: Decimal | None


class FinanceTrendPayload(BaseModel):
    week_starts_on: date
    net_spent: Decimal


class FinanceDashboardPayload(BaseModel):
    year: int
    month: int
    income: IncomePayload | None
    income_history: list[IncomePayload]
    budget: BudgetPayload | None
    gross_expenses: Decimal
    gross_refunds: Decimal
    net_expenses: Decimal
    remaining_income: Decimal | None
    savings_target: Decimal | None
    categories: list[FinanceCategoryPayload]
    trend: list[FinanceTrendPayload]
    transactions: list[ExpenseTransactionPayload]


class ExpenseMutationPayload(BaseModel):
    operation: str
    transactions: list[ExpenseTransactionPayload]


class IncomeMutationPayload(BaseModel):
    operation: str
    income: IncomePayload


class BudgetMutationPayload(BaseModel):
    operation: str
    budget: BudgetPayload
