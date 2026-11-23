from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request

from harle_api.dependencies import get_account_runtime, get_runtime
from harle_api.payloads import (
    BudgetMutationPayload,
    CreateExpensePayload,
    CreateInstallmentExpensePayload,
    ExpenseMutationPayload,
    FinanceDashboardPayload,
    IncomeMutationPayload,
    SaveBudgetPayload,
    SaveIncomePayload,
    UpdateExpensePayload,
)
from harle_api.settings import ApiSettings, get_settings
from harle_domain.expenses import (
    ExpenseTransaction,
    FinanceBudgetSettings,
    IncomeHistoryEntry,
    MonthlyFinanceAnalysis,
)
from harle_services.expenses import CreateExpense, CreateInstallmentExpense
from harle_utils import (
    AccessDeniedError,
    ExpenseNotFoundError,
    InvalidCsrfError,
    InvalidFinanceError,
    MissingProfileError,
)

router = APIRouter(prefix="/api/finances")


@dataclass(frozen=True, slots=True)
class WebFinanceContext:
    user_id: UUID
    timezone: str


@router.get("/dashboard", response_model=FinanceDashboardPayload)
async def get_finance_dashboard(
    request: Request,
    year: Annotated[int, Query(ge=1, le=9999)],
    month: Annotated[int, Query(ge=1, le=12)],
) -> FinanceDashboardPayload:
    context = await _context(request)
    try:
        analysis = await get_runtime(request).finances.analyze_month(
            user_id=context.user_id,
            year=year,
            month=month,
        )
    except ValueError as exc:
        raise InvalidFinanceError from exc
    history = await get_runtime(request).finances.income_history(
        user_id=context.user_id,
    )
    return _dashboard_payload(analysis, history)


@router.post("/income", response_model=IncomeMutationPayload)
async def post_income(
    payload: SaveIncomePayload,
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> IncomeMutationPayload:
    context = await _mutation_context(request, x_csrf_token)
    try:
        income = await get_runtime(request).finances.save_income(
            user_id=context.user_id,
            amount=payload.amount,
            effective_on=payload.effective_on,
        )
    except ValueError as exc:
        raise InvalidFinanceError from exc
    return IncomeMutationPayload.model_validate(
        {"operation": "saved", "income": _income_payload(income)},
    )


@router.post("/budget", response_model=BudgetMutationPayload)
async def post_budget(
    payload: SaveBudgetPayload,
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> BudgetMutationPayload:
    context = await _mutation_context(request, x_csrf_token)
    try:
        budget = await get_runtime(request).finances.save_budget(
            user_id=context.user_id,
            percentages=payload.percentages(),
        )
    except (KeyError, ValueError) as exc:
        raise InvalidFinanceError from exc
    return BudgetMutationPayload.model_validate(
        {"operation": "saved", "budget": _budget_payload(budget)},
    )


@router.post("/expenses", response_model=ExpenseMutationPayload)
async def post_expense(
    payload: CreateExpensePayload,
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> ExpenseMutationPayload:
    context = await _mutation_context(request, x_csrf_token)
    try:
        result = await get_runtime(request).expenses.add_expense(
            user_id=context.user_id,
            timezone_name=context.timezone,
            expense=CreateExpense(
                amount=payload.amount,
                category=payload.category,
                description=payload.description,
                transaction_date=payload.transaction_date,
                entry_type=payload.entry_type,
            ),
        )
    except ValueError as exc:
        raise InvalidFinanceError from exc
    return _expense_mutation("created", result.transactions)


@router.post("/expenses/installments", response_model=ExpenseMutationPayload)
async def post_installment_expense(
    payload: CreateInstallmentExpensePayload,
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> ExpenseMutationPayload:
    context = await _mutation_context(request, x_csrf_token)
    try:
        result = await get_runtime(request).expenses.add_installments(
            user_id=context.user_id,
            timezone_name=context.timezone,
            purchase=CreateInstallmentExpense(
                expense=CreateExpense(
                    amount=payload.total_amount,
                    category=payload.category,
                    description=payload.description,
                    transaction_date=payload.transaction_date,
                ),
                installment_count=payload.installment_count,
            ),
        )
    except ValueError as exc:
        raise InvalidFinanceError from exc
    return _expense_mutation("created", result.transactions)


@router.post("/expenses/{transaction_id}", response_model=ExpenseMutationPayload)
async def post_expense_update(
    transaction_id: UUID,
    payload: UpdateExpensePayload,
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> ExpenseMutationPayload:
    context = await _mutation_context(request, x_csrf_token)
    try:
        transactions = await get_runtime(request).expenses.update(
            user_id=context.user_id,
            transaction_id=transaction_id,
            changes=payload.to_changes(),
        )
    except ValueError as exc:
        raise InvalidFinanceError from exc
    if not transactions:
        raise ExpenseNotFoundError
    return _expense_mutation("updated", transactions)


@router.post(
    "/expenses/{transaction_id}/delete",
    response_model=ExpenseMutationPayload,
)
async def post_expense_delete(
    transaction_id: UUID,
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> ExpenseMutationPayload:
    context = await _mutation_context(request, x_csrf_token)
    transactions = await get_runtime(request).expenses.delete(
        user_id=context.user_id,
        transaction_id=transaction_id,
    )
    if not transactions:
        raise ExpenseNotFoundError
    return _expense_mutation("deleted", transactions)


async def _context(request: Request) -> WebFinanceContext:
    settings = get_settings()
    session = await get_account_runtime(request).sessions.resolve(
        request.cookies.get(settings.SESSION_COOKIE_NAME),
    )
    runtime = get_runtime(request)
    if (
        session.user_id
        == runtime.tools.access_policy.legacy_google_sheets_user_id
    ):
        raise AccessDeniedError
    profile = await runtime.profiles.get(user_id=session.user_id)
    if profile is None:
        raise MissingProfileError
    return WebFinanceContext(user_id=session.user_id, timezone=profile.timezone)


async def _mutation_context(
    request: Request,
    csrf_token: str | None,
) -> WebFinanceContext:
    settings = get_settings()
    _require_frontend_origin(request, settings)
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    sessions = get_account_runtime(request).sessions
    await sessions.resolve(token)
    sessions.require_csrf(session_token=token or "", csrf_token=csrf_token)
    return await _context(request)


def _dashboard_payload(
    analysis: MonthlyFinanceAnalysis,
    history: Sequence[IncomeHistoryEntry],
) -> FinanceDashboardPayload:
    return FinanceDashboardPayload.model_validate(
        {
            "year": analysis.year,
            "month": analysis.month,
            "income": (
                _income_payload(analysis.income)
                if analysis.income is not None
                else None
            ),
            "income_history": [_income_payload(entry) for entry in history],
            "budget": (
                _budget_payload(analysis.budget)
                if analysis.budget is not None
                else None
            ),
            "gross_expenses": analysis.gross_expenses,
            "gross_refunds": analysis.gross_refunds,
            "net_expenses": analysis.net_expenses,
            "remaining_income": analysis.remaining_income,
            "savings_target": analysis.savings_target,
            "categories": [
                {
                    "category": item.category,
                    "actual_amount": item.actual_amount,
                    "income_percentage": item.income_percentage,
                    "budget_percentage": item.budget_percentage,
                    "budget_amount": item.budget_amount,
                    "budget_used_percentage": item.budget_used_percentage,
                }
                for item in analysis.categories
            ],
            "trend": [
                {
                    "week_starts_on": item.week_starts_on,
                    "net_spent": item.net_spent,
                }
                for item in analysis.trend
            ],
            "transactions": [
                _transaction_payload(transaction)
                for transaction in analysis.transactions
            ],
        },
    )


def _income_payload(income: IncomeHistoryEntry) -> dict[str, object]:
    return {
        "income_id": income.id,
        "amount": income.amount,
        "currency": income.currency,
        "effective_on": income.effective_on,
        "created_at": income.created_at,
        "updated_at": income.updated_at,
    }


def _budget_payload(budget: FinanceBudgetSettings) -> dict[str, object]:
    return {
        "allocations": [
            {
                "category": allocation.category,
                "percentage": allocation.percentage,
            }
            for allocation in budget.allocations
        ],
        "created_at": budget.created_at,
        "updated_at": budget.updated_at,
    }


def _expense_mutation(
    operation: str,
    transactions: Sequence[ExpenseTransaction],
) -> ExpenseMutationPayload:
    return ExpenseMutationPayload.model_validate(
        {
            "operation": operation,
            "transactions": [
                _transaction_payload(transaction) for transaction in transactions
            ],
        },
    )


def _transaction_payload(transaction: ExpenseTransaction) -> dict[str, object]:
    return {
        "transaction_id": transaction.id,
        "entry_type": transaction.entry_type,
        "amount": transaction.amount,
        "currency": transaction.currency,
        "category": transaction.category,
        "transaction_date": transaction.transaction_date,
        "description": transaction.description,
        "installment_group_id": transaction.installment_group_id,
        "installment_number": transaction.installment_number,
        "installment_count": transaction.installment_count,
    }


def _require_frontend_origin(request: Request, settings: ApiSettings) -> None:
    origin = request.headers.get("origin")
    if origin is None or origin not in settings.FRONTEND_ORIGINS:
        raise InvalidCsrfError
