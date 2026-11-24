from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from harle_domain.expenses import (
    ARS_CURRENCY,
    ExpenseCategory,
    ExpenseDetails,
    ExpenseEntryType,
    ExpenseInstallment,
    ExpenseRepository,
    ExpenseSummary,
    ExpenseTimestamps,
    ExpenseTransaction,
    installment_dates,
    resolve_transaction_date,
    split_installment_amount,
    summarize_expenses,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ExpenseCreationResult:
    transactions: Sequence[ExpenseTransaction]
    used_previous_day_rule: bool


@dataclass(frozen=True, slots=True)
class CreateExpense:
    amount: Decimal
    category: ExpenseCategory
    description: str
    transaction_date: date | None
    entry_type: ExpenseEntryType = ExpenseEntryType.EXPENSE


@dataclass(frozen=True, slots=True)
class CreateInstallmentExpense:
    expense: CreateExpense
    installment_count: int

    def __post_init__(self) -> None:
        if self.expense.entry_type is ExpenseEntryType.REFUND:
            raise ValueError("Refunds cannot be split into installments.")


@dataclass(frozen=True, slots=True)
class ExpenseChanges:
    amount: Decimal | None = None
    entry_type: ExpenseEntryType | None = None
    category: ExpenseCategory | None = None
    transaction_date: date | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        if all(
            value is None
            for value in (
                self.amount,
                self.entry_type,
                self.category,
                self.transaction_date,
                self.description,
            )
        ):
            raise ValueError("At least one expense change is required.")
        if self.description is not None and not self.description.strip():
            raise ValueError("Expense description cannot be empty.")


@dataclass(frozen=True, slots=True)
class ExpenseService:
    repository: ExpenseRepository
    clock: Callable[[], datetime] = utc_now

    async def add_expense(
        self,
        *,
        user_id: UUID,
        timezone_name: str,
        expense: CreateExpense,
    ) -> ExpenseCreationResult:
        now = self._now()
        resolved_date = resolve_transaction_date(
            current_time=now,
            timezone_name=timezone_name,
            requested_date=expense.transaction_date,
        )
        transaction = _new_transaction(
            user_id=user_id,
            expense=expense,
            transaction_date=resolved_date.value,
            now=now,
        )
        created = await self.repository.create(
            user_id=user_id,
            transactions=[transaction],
        )
        return ExpenseCreationResult(
            transactions=created,
            used_previous_day_rule=resolved_date.used_previous_day_rule,
        )

    async def add_installments(
        self,
        *,
        user_id: UUID,
        timezone_name: str,
        purchase: CreateInstallmentExpense,
    ) -> ExpenseCreationResult:
        now = self._now()
        resolved_date = resolve_transaction_date(
            current_time=now,
            timezone_name=timezone_name,
            requested_date=purchase.expense.transaction_date,
        )
        amounts = split_installment_amount(
            purchase.expense.amount,
            purchase.installment_count,
        )
        dates = installment_dates(
            resolved_date.value,
            purchase.installment_count,
        )
        group_id = uuid4()
        transactions = [
            _new_transaction(
                user_id=user_id,
                expense=replace(purchase.expense, amount=amount),
                transaction_date=installment_date,
                now=now,
                installment=ExpenseInstallment(
                    group_id=group_id,
                    number=index + 1,
                    count=purchase.installment_count,
                ),
            )
            for index, (amount, installment_date) in enumerate(
                zip(amounts, dates, strict=True),
            )
        ]
        created = await self.repository.create(
            user_id=user_id,
            transactions=transactions,
        )
        return ExpenseCreationResult(
            transactions=created,
            used_previous_day_rule=resolved_date.used_previous_day_rule,
        )

    async def list_for_date(
        self,
        *,
        user_id: UUID,
        transaction_date: date,
    ) -> Sequence[ExpenseTransaction]:
        return await self.repository.list_for_date(
            user_id=user_id,
            transaction_date=transaction_date,
        )

    async def summarize_month(
        self,
        *,
        user_id: UUID,
        year: int,
        month: int,
    ) -> ExpenseSummary:
        month_start = date(year, month, 1)
        next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        transactions = await self.repository.list_for_range(
            user_id=user_id,
            start_date=month_start,
            end_date=next_month - timedelta(days=1),
        )
        return summarize_expenses(transactions, year=year, month=month)

    async def update(
        self,
        *,
        user_id: UUID,
        timezone_name: str,
        transaction_id: UUID,
        changes: ExpenseChanges,
    ) -> Sequence[ExpenseTransaction]:
        related = await self.repository.get_related(
            user_id=user_id,
            transaction_id=transaction_id,
        )
        if not related:
            return []
        now = self._now()
        ordered = sorted(
            related,
            key=lambda transaction: transaction.installment_number or 1,
        )
        selected = next(
            transaction for transaction in ordered if transaction.id == transaction_id
        )
        if changes.transaction_date is not None:
            date_shift = changes.transaction_date - selected.transaction_date
            first_date = ordered[0].transaction_date + date_shift
            resolve_transaction_date(
                current_time=now,
                timezone_name=timezone_name,
                requested_date=first_date,
            )
        replacements = _apply_changes(
            ordered,
            changes,
            now,
            selected_transaction=selected,
        )
        return await self.repository.update_related(
            user_id=user_id,
            transaction_id=transaction_id,
            transactions=replacements,
        )

    async def delete(
        self,
        *,
        user_id: UUID,
        transaction_id: UUID,
    ) -> Sequence[ExpenseTransaction]:
        return await self.repository.delete_related(
            user_id=user_id,
            transaction_id=transaction_id,
        )

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Expense service clock must return a timezone-aware time.")
        return now


def _new_transaction(
    *,
    user_id: UUID,
    expense: CreateExpense,
    transaction_date: date,
    now: datetime,
    installment: ExpenseInstallment | None = None,
) -> ExpenseTransaction:
    return ExpenseTransaction(
        id=uuid4(),
        user_id=user_id,
        details=ExpenseDetails(
            entry_type=expense.entry_type,
            amount=expense.amount,
            currency=ARS_CURRENCY,
            category=expense.category,
            transaction_date=transaction_date,
            description=expense.description.strip(),
        ),
        installment=installment,
        timestamps=ExpenseTimestamps(
            created_at=now,
            updated_at=now,
        ),
    )


def _apply_changes(
    transactions: Sequence[ExpenseTransaction],
    changes: ExpenseChanges,
    now: datetime,
    *,
    selected_transaction: ExpenseTransaction,
) -> Sequence[ExpenseTransaction]:
    first = transactions[0]
    count = first.installment_count
    entry_type = changes.entry_type or first.entry_type
    if count is not None and entry_type is ExpenseEntryType.REFUND:
        raise ValueError("An installment purchase cannot become a refund.")
    amounts = _updated_amounts(transactions, changes.amount)
    dates = _updated_dates(
        transactions,
        changes.transaction_date,
        selected_transaction=selected_transaction,
    )
    return [
        replace(
            transaction,
            details=replace(
                transaction.details,
                entry_type=entry_type,
                amount=amount,
                category=changes.category or transaction.category,
                transaction_date=transaction_date,
                description=(
                    changes.description.strip()
                    if changes.description is not None
                    else transaction.description
                ),
            ),
            timestamps=replace(
                transaction.timestamps,
                updated_at=now,
            ),
        )
        for transaction, amount, transaction_date in zip(
            transactions,
            amounts,
            dates,
            strict=True,
        )
    ]


def _updated_amounts(
    transactions: Sequence[ExpenseTransaction],
    total_amount: Decimal | None,
) -> Sequence[Decimal]:
    if total_amount is None:
        return [transaction.amount for transaction in transactions]
    installment_count = transactions[0].installment_count
    if installment_count is None:
        return [total_amount]
    return split_installment_amount(total_amount, installment_count)


def _updated_dates(
    transactions: Sequence[ExpenseTransaction],
    requested_date: date | None,
    *,
    selected_transaction: ExpenseTransaction,
) -> Sequence[date]:
    if requested_date is None:
        return [transaction.transaction_date for transaction in transactions]
    installment_count = transactions[0].installment_count
    if installment_count is None:
        return [requested_date]
    date_shift = requested_date - selected_transaction.transaction_date
    return [transaction.transaction_date + date_shift for transaction in transactions]
