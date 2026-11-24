from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    CENT,
    ExpenseCategory,
    ExpenseCategoryTotal,
    ExpenseEntryType,
    ExpenseSummary,
    ExpenseTransaction,
)


@dataclass(frozen=True, slots=True)
class ResolvedTransactionDate:
    value: date
    used_previous_day_rule: bool


def resolve_transaction_date(
    *,
    current_time: datetime,
    timezone_name: str,
    requested_date: date | None,
) -> ResolvedTransactionDate:
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("Current time must include a timezone.")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {timezone_name}.") from exc
    local_time = current_time.astimezone(timezone)
    if requested_date is not None:
        if requested_date > local_time.date():
            raise ValueError("Expense date cannot be in the future.")
        return ResolvedTransactionDate(requested_date, False)
    use_previous_day = local_time.hour <= 4
    transaction_date = local_time.date()
    if use_previous_day:
        transaction_date -= timedelta(days=1)
    return ResolvedTransactionDate(transaction_date, use_previous_day)


def split_installment_amount(
    total: Decimal,
    installment_count: int,
) -> Sequence[Decimal]:
    if not total.is_finite() or total <= 0 or total.quantize(CENT) != total:
        raise ValueError(
            "Installment total must be positive with at most two decimals.",
        )
    if not 2 <= installment_count <= 12:
        raise ValueError("Installment count must be between 2 and 12.")
    total_cents = int(total / CENT)
    base_cents, remainder = divmod(total_cents, installment_count)
    if base_cents == 0:
        raise ValueError("Each installment must be at least ARS 0.01.")
    return [
        Decimal(base_cents + (index < remainder)) * CENT
        for index in range(installment_count)
    ]


def installment_dates(
    first_date: date,
    installment_count: int,
) -> Sequence[date]:
    if not 2 <= installment_count <= 12:
        raise ValueError("Installment count must be between 2 and 12.")
    dates = [first_date]
    for offset in range(1, installment_count):
        month_index = first_date.month - 1 + offset
        year = first_date.year + month_index // 12
        month = month_index % 12 + 1
        dates.append(date(year, month, 1))
    return dates


def summarize_expenses(
    transactions: Sequence[ExpenseTransaction],
    *,
    year: int,
    month: int,
) -> ExpenseSummary:
    date(year, month, 1)
    totals = {category: Decimal("0.00") for category in ExpenseCategory}
    for transaction in transactions:
        contribution = transaction.amount
        if transaction.entry_type is ExpenseEntryType.REFUND:
            contribution = -contribution
        totals[transaction.category] += contribution
    category_totals = [
        ExpenseCategoryTotal(category, totals[category])
        for category in ExpenseCategory
        if totals[category] != 0
    ]
    return ExpenseSummary(
        year=year,
        month=month,
        category_totals=category_totals,
        total=sum(
            (category_total.amount for category_total in category_totals),
            Decimal("0.00"),
        ),
    )
