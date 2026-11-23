from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, TypeVar
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harle_domain.expenses import (
    ARS_CURRENCY,
    BudgetCategory,
    ExpenseCategory,
    ExpenseEntryType,
    ExpenseSummary,
    ExpenseTransaction,
    FinanceBudgetSettings,
    IncomeHistoryEntry,
    MonthlyFinanceAnalysis,
)
from harle_domain.tools import (
    ToolCallResult,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolFamily,
    ToolHandler,
)
from harle_services.expenses import (
    CreateExpense,
    CreateInstallmentExpense,
    ExpenseChanges,
    ExpenseService,
    FinancePlanningService,
)

from .registry import ToolFamilyRegistration, ToolHandlerFactory

Money = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)]
InstallmentCount = Annotated[int, Field(ge=2, le=12)]
MonthNumber = Annotated[int, Field(ge=1, le=12)]
YearNumber = Annotated[int, Field(ge=1, le=9999)]
ModelT = TypeVar("ModelT", bound=BaseModel)


class ExpenseInput(BaseModel):
    amount: Money
    category: ExpenseCategory
    description: str = Field(min_length=1, max_length=500)
    transaction_date: date | None = None

    model_config = ConfigDict(extra="forbid")


class AddExpenseArgs(ExpenseInput):
    pass


class AddRefundArgs(ExpenseInput):
    pass


class AddInstallmentExpenseArgs(BaseModel):
    total_amount: Money
    category: ExpenseCategory
    description: str = Field(min_length=1, max_length=500)
    transaction_date: date | None = None
    installment_count: InstallmentCount

    model_config = ConfigDict(extra="forbid")


class ListExpensesArgs(BaseModel):
    transaction_date: date

    model_config = ConfigDict(extra="forbid")


class SummarizeExpensesArgs(BaseModel):
    year: YearNumber
    month: MonthNumber

    model_config = ConfigDict(extra="forbid")


class UpdateExpenseArgs(BaseModel):
    transaction_id: UUID
    amount: Money | None = None
    entry_type: ExpenseEntryType | None = None
    category: ExpenseCategory | None = None
    transaction_date: date | None = None
    description: str | None = Field(default=None, min_length=1, max_length=500)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def require_change(self) -> "UpdateExpenseArgs":
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


class DeleteExpenseArgs(BaseModel):
    transaction_id: UUID

    model_config = ConfigDict(extra="forbid")


class SetMonthlyIncomeArgs(BaseModel):
    amount: Money
    effective_on: date | None = None

    model_config = ConfigDict(extra="forbid")


class SetBudgetArgs(BaseModel):
    rent: Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)]
    essential_services: Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)]
    non_essential_services: Annotated[
        Decimal,
        Field(ge=0, le=100, decimal_places=2),
    ]
    home: Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)]
    transport: Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)]
    outings: Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)]
    shopping: Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)]
    other: Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)]
    savings: Annotated[Decimal, Field(ge=0, le=100, decimal_places=2)]

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def require_total(self) -> "SetBudgetArgs":
        if sum(self.percentages().values(), Decimal("0.00")) != Decimal("100.00"):
            raise ValueError("Budget percentages must total exactly 100.")
        return self

    def percentages(self) -> dict[BudgetCategory, Decimal]:
        return {category: getattr(self, category.value) for category in BudgetCategory}


class MonthlyFinanceSummaryArgs(BaseModel):
    year: YearNumber
    month: MonthNumber

    model_config = ConfigDict(extra="forbid")


FAMILY = ToolFamily.INTERNAL_EXPENSES

SHARED_INSTRUCTIONS = """For every internal expense tool:
- Amounts are Argentine pesos and categories are rent, essential_services, non_essential_services, home, transport, outings, shopping, or other.
- Never accept a user-selected expense date after the user's current local date. Automatically generated later installments may fall in future months.
- When no date is supplied, transactions from 00:00 through 04:59 use the previous local day; tell the user when used_previous_day_rule is true.
- Read results include transaction UUIDs. Updating or deleting an installment UUID affects its entire installment group.
- Income history is effective-dated. Budgets are current settings and always allocate exactly 100%, including savings.
- Savings is not an expense category. Report remaining income after net expenses as actual savings against its configured budget; it may exceed the target or become negative.
- Deletion is permanent. Never expose or request external spreadsheet details."""

DEFINITIONS = (
    ToolDefinition(
        name="add_expense",
        family=FAMILY,
        description="Add one ARS expense, optionally on a specified date.",
        argument_model=AddExpenseArgs,
        effect=ToolEffect.MODIFY,
        can_run_concurrently=False,
    ),
    ToolDefinition(
        name="add_refund",
        family=FAMILY,
        description="Add one ARS refund that subtracts from expense summaries.",
        argument_model=AddRefundArgs,
        effect=ToolEffect.MODIFY,
        can_run_concurrently=False,
    ),
    ToolDefinition(
        name="add_installment_expense",
        family=FAMILY,
        description=(
            "Split one ARS purchase into 2 to 12 exact monthly installments. "
            "The first uses the selected date and later installments use day 1."
        ),
        argument_model=AddInstallmentExpenseArgs,
        effect=ToolEffect.MODIFY,
        can_run_concurrently=False,
    ),
    ToolDefinition(
        name="list_expenses",
        family=FAMILY,
        description="List expenses and refunds for one calendar date.",
        argument_model=ListExpensesArgs,
        effect=ToolEffect.READ,
        can_run_concurrently=True,
    ),
    ToolDefinition(
        name="summarize_expenses",
        family=FAMILY,
        description="Summarize one month's expenses by category.",
        argument_model=SummarizeExpensesArgs,
        effect=ToolEffect.READ,
        can_run_concurrently=True,
    ),
    ToolDefinition(
        name="update_expense",
        family=FAMILY,
        description=(
            "Partially update a transaction by UUID. An installment UUID updates "
            "the entire purchase while preserving its installment count."
        ),
        argument_model=UpdateExpenseArgs,
        effect=ToolEffect.MODIFY,
        can_run_concurrently=False,
    ),
    ToolDefinition(
        name="delete_expense",
        family=FAMILY,
        description=(
            "Permanently delete a transaction by UUID. An installment UUID "
            "deletes the entire purchase."
        ),
        argument_model=DeleteExpenseArgs,
        effect=ToolEffect.MODIFY,
        can_run_concurrently=False,
    ),
)

PLANNING_DEFINITIONS = (
    ToolDefinition(
        name="get_finance_settings",
        family=FAMILY,
        description="Read the user's income history and current budget allocation.",
        argument_model=MonthlyFinanceSummaryArgs,
        effect=ToolEffect.READ,
        can_run_concurrently=True,
    ),
    ToolDefinition(
        name="set_monthly_income",
        family=FAMILY,
        description="Save monthly net ARS income effective on a date.",
        argument_model=SetMonthlyIncomeArgs,
        effect=ToolEffect.MODIFY,
        can_run_concurrently=False,
    ),
    ToolDefinition(
        name="set_finance_budget",
        family=FAMILY,
        description=(
            "Replace all expense-category and savings percentages; they must total 100."
        ),
        argument_model=SetBudgetArgs,
        effect=ToolEffect.MODIFY,
        can_run_concurrently=False,
    ),
    ToolDefinition(
        name="get_monthly_finance_summary",
        family=FAMILY,
        description=(
            "Analyze income, net spending, category budgets, remaining income, "
            "and savings target for one month."
        ),
        argument_model=MonthlyFinanceSummaryArgs,
        effect=ToolEffect.READ,
        can_run_concurrently=True,
    ),
)


def create_internal_expenses_registration(
    service: ExpenseService,
    planning: FinancePlanningService | None = None,
) -> ToolFamilyRegistration:
    def build_handlers(
        context: ToolExecutionContext,
    ) -> Mapping[str, ToolHandler]:
        async def add_expense(args: BaseModel) -> ToolCallResult:
            context.require_family(FAMILY)
            validated = _require_model(args, AddExpenseArgs)
            result = await service.add_expense(
                user_id=context.user_id,
                timezone_name=context.timezone,
                expense=_create_expense(validated),
            )
            return ToolCallResult(
                called_tool_name="add_expense",
                result=_creation_payload(
                    result.transactions,
                    result.used_previous_day_rule,
                ),
            )

        async def add_refund(args: BaseModel) -> ToolCallResult:
            context.require_family(FAMILY)
            validated = _require_model(args, AddRefundArgs)
            result = await service.add_expense(
                user_id=context.user_id,
                timezone_name=context.timezone,
                expense=_create_expense(
                    validated,
                    entry_type=ExpenseEntryType.REFUND,
                ),
            )
            return ToolCallResult(
                called_tool_name="add_refund",
                result=_creation_payload(
                    result.transactions,
                    result.used_previous_day_rule,
                ),
            )

        async def add_installments(args: BaseModel) -> ToolCallResult:
            context.require_family(FAMILY)
            validated = _require_model(args, AddInstallmentExpenseArgs)
            result = await service.add_installments(
                user_id=context.user_id,
                timezone_name=context.timezone,
                purchase=CreateInstallmentExpense(
                    expense=CreateExpense(
                        amount=validated.total_amount,
                        category=validated.category,
                        description=validated.description,
                        transaction_date=validated.transaction_date,
                    ),
                    installment_count=validated.installment_count,
                ),
            )
            return ToolCallResult(
                called_tool_name="add_installment_expense",
                result=_creation_payload(
                    result.transactions,
                    result.used_previous_day_rule,
                ),
            )

        async def list_expenses(args: BaseModel) -> ToolCallResult:
            context.require_family(FAMILY)
            validated = _require_model(args, ListExpensesArgs)
            transactions = await service.list_for_date(
                user_id=context.user_id,
                transaction_date=validated.transaction_date,
            )
            return ToolCallResult(
                called_tool_name="list_expenses",
                result={
                    "ok": True,
                    "transaction_date": validated.transaction_date.isoformat(),
                    "currency": ARS_CURRENCY,
                    "transactions": [
                        _transaction_payload(transaction)
                        for transaction in transactions
                    ],
                },
            )

        async def summarize(args: BaseModel) -> ToolCallResult:
            context.require_family(FAMILY)
            validated = _require_model(args, SummarizeExpensesArgs)
            summary = await service.summarize_month(
                user_id=context.user_id,
                year=validated.year,
                month=validated.month,
            )
            return ToolCallResult(
                called_tool_name="summarize_expenses",
                result=_summary_payload(summary),
            )

        async def update(args: BaseModel) -> ToolCallResult:
            context.require_family(FAMILY)
            validated = _require_model(args, UpdateExpenseArgs)
            transactions = await service.update(
                user_id=context.user_id,
                timezone_name=context.timezone,
                transaction_id=validated.transaction_id,
                changes=validated.to_changes(),
            )
            return ToolCallResult(
                called_tool_name="update_expense",
                result=_mutation_payload(transactions, "updated"),
            )

        async def delete(args: BaseModel) -> ToolCallResult:
            context.require_family(FAMILY)
            validated = _require_model(args, DeleteExpenseArgs)
            transactions = await service.delete(
                user_id=context.user_id,
                transaction_id=validated.transaction_id,
            )
            return ToolCallResult(
                called_tool_name="delete_expense",
                result=_mutation_payload(transactions, "deleted"),
            )

        handlers: dict[str, ToolHandler] = {
            "add_expense": add_expense,
            "add_refund": add_refund,
            "add_installment_expense": add_installments,
            "list_expenses": list_expenses,
            "summarize_expenses": summarize,
            "update_expense": update,
            "delete_expense": delete,
        }
        if planning is None:
            return handlers

        async def get_settings(args: BaseModel) -> ToolCallResult:
            context.require_family(FAMILY)
            validated = _require_model(args, MonthlyFinanceSummaryArgs)
            analysis = await planning.analyze_month(
                user_id=context.user_id,
                year=validated.year,
                month=validated.month,
                timezone_name=context.timezone,
            )
            history = await planning.income_history(user_id=context.user_id)
            return ToolCallResult(
                called_tool_name="get_finance_settings",
                result={
                    "ok": True,
                    "income": _income_payload(analysis.income),
                    "income_history": [_income_payload(item) for item in history],
                    "budget": _budget_payload(analysis.budget),
                },
            )

        async def set_income(args: BaseModel) -> ToolCallResult:
            context.require_family(FAMILY)
            validated = _require_model(args, SetMonthlyIncomeArgs)
            effective_on = (
                validated.effective_on
                or datetime.now(
                    ZoneInfo(context.timezone),
                ).date()
            )
            income = await planning.save_income(
                user_id=context.user_id,
                amount=validated.amount,
                effective_on=effective_on,
                timezone_name=context.timezone,
            )
            return ToolCallResult(
                called_tool_name="set_monthly_income",
                result={"ok": True, "income": _income_payload(income)},
            )

        async def set_budget(args: BaseModel) -> ToolCallResult:
            context.require_family(FAMILY)
            validated = _require_model(args, SetBudgetArgs)
            budget = await planning.save_budget(
                user_id=context.user_id,
                percentages=validated.percentages(),
            )
            return ToolCallResult(
                called_tool_name="set_finance_budget",
                result={"ok": True, "budget": _budget_payload(budget)},
            )

        async def finance_summary(args: BaseModel) -> ToolCallResult:
            context.require_family(FAMILY)
            validated = _require_model(args, MonthlyFinanceSummaryArgs)
            analysis = await planning.analyze_month(
                user_id=context.user_id,
                year=validated.year,
                month=validated.month,
                timezone_name=context.timezone,
            )
            return ToolCallResult(
                called_tool_name="get_monthly_finance_summary",
                result=_finance_analysis_payload(analysis),
            )

        handlers.update(
            {
                "get_finance_settings": get_settings,
                "set_monthly_income": set_income,
                "set_finance_budget": set_budget,
                "get_monthly_finance_summary": finance_summary,
            },
        )
        return handlers

    definitions = DEFINITIONS + (PLANNING_DEFINITIONS if planning is not None else ())
    return _internal_registration(build_handlers, definitions)


def _internal_registration(
    handler_factory: ToolHandlerFactory,
    definitions: Sequence[ToolDefinition],
) -> ToolFamilyRegistration:
    return ToolFamilyRegistration(
        family=FAMILY,
        instructions=SHARED_INSTRUCTIONS,
        definitions=definitions,
        handler_factory=handler_factory,
    )


def _create_expense(
    args: ExpenseInput,
    *,
    entry_type: ExpenseEntryType = ExpenseEntryType.EXPENSE,
) -> CreateExpense:
    return CreateExpense(
        amount=args.amount,
        category=args.category,
        description=args.description,
        transaction_date=args.transaction_date,
        entry_type=entry_type,
    )


def _creation_payload(
    transactions: Sequence[ExpenseTransaction],
    used_previous_day_rule: bool,
) -> Mapping[str, object]:
    return {
        "ok": True,
        "currency": ARS_CURRENCY,
        "used_previous_day_rule": used_previous_day_rule,
        "transactions": [
            _transaction_payload(transaction) for transaction in transactions
        ],
    }


def _mutation_payload(
    transactions: Sequence[ExpenseTransaction],
    operation: str,
) -> Mapping[str, object]:
    transaction_list = list(transactions)
    return {
        "ok": bool(transaction_list),
        "operation": operation,
        "reason": None if transaction_list else "Expense transaction was not found.",
        "transactions": [
            _transaction_payload(transaction) for transaction in transaction_list
        ],
    }


def _transaction_payload(
    transaction: ExpenseTransaction,
) -> Mapping[str, object]:
    return {
        "transaction_id": str(transaction.id),
        "entry_type": transaction.entry_type.value,
        "amount": str(transaction.amount),
        "currency": transaction.currency,
        "category": transaction.category.value,
        "transaction_date": transaction.transaction_date.isoformat(),
        "description": transaction.description,
        "installment_group_id": (
            str(transaction.installment_group_id)
            if transaction.installment_group_id is not None
            else None
        ),
        "installment_number": transaction.installment_number,
        "installment_count": transaction.installment_count,
    }


def _summary_payload(summary: ExpenseSummary) -> Mapping[str, object]:
    return {
        "ok": True,
        "year": summary.year,
        "month": summary.month,
        "currency": ARS_CURRENCY,
        "category_totals": [
            {
                "category": category_total.category.value,
                "amount": str(category_total.amount),
            }
            for category_total in summary.category_totals
        ],
        "total": str(summary.total),
    }


def _income_payload(
    income: IncomeHistoryEntry | None,
) -> Mapping[str, object] | None:
    if income is None:
        return None
    return {
        "income_id": str(income.id),
        "amount": str(income.amount),
        "currency": income.currency,
        "effective_on": income.effective_on.isoformat(),
    }


def _budget_payload(
    budget: FinanceBudgetSettings | None,
) -> Mapping[str, object] | None:
    if budget is None:
        return None
    return {
        allocation.category.value: str(allocation.percentage)
        for allocation in budget.allocations
    }


def _finance_analysis_payload(
    analysis: MonthlyFinanceAnalysis,
) -> Mapping[str, object]:
    return {
        "ok": True,
        "year": analysis.year,
        "month": analysis.month,
        "income": _income_payload(analysis.income),
        "budget": _budget_payload(analysis.budget),
        "gross_expenses": str(analysis.gross_expenses),
        "gross_refunds": str(analysis.gross_refunds),
        "net_expenses": str(analysis.net_expenses),
        "remaining_income": (
            str(analysis.remaining_income)
            if analysis.remaining_income is not None
            else None
        ),
        "savings_target": (
            str(analysis.savings_target)
            if analysis.savings_target is not None
            else None
        ),
        "categories": [
            {
                "category": category.category.value,
                "actual_amount": (
                    str(category.actual_amount)
                    if category.actual_amount is not None
                    else None
                ),
                "income_percentage": (
                    str(category.income_percentage)
                    if category.income_percentage is not None
                    else None
                ),
                "budget_percentage": (
                    str(category.budget_percentage)
                    if category.budget_percentage is not None
                    else None
                ),
                "budget_amount": (
                    str(category.budget_amount)
                    if category.budget_amount is not None
                    else None
                ),
                "budget_used_percentage": (
                    str(category.budget_used_percentage)
                    if category.budget_used_percentage is not None
                    else None
                ),
            }
            for category in analysis.categories
        ],
    }


def _require_model(value: BaseModel, expected: type[ModelT]) -> ModelT:
    if not isinstance(value, expected):
        raise TypeError("Validated tool arguments do not match the tool definition.")
    return value
