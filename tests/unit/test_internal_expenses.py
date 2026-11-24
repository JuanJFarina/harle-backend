import asyncio
from collections.abc import MutableMapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from harle_domain.expenses import (
    ExpenseCategory,
    ExpenseEntryType,
    ExpenseTransaction,
    installment_dates,
    resolve_transaction_date,
    split_installment_amount,
)
from harle_services.expenses import (
    CreateExpense,
    CreateInstallmentExpense,
    ExpenseChanges,
    ExpenseService,
)

NOW = datetime(2026, 8, 31, 5, 30, tzinfo=timezone.utc)


class FakeExpenseRepository:
    def __init__(self) -> None:
        self.transactions: MutableMapping[UUID, ExpenseTransaction] = {}

    async def create(
        self,
        *,
        user_id: UUID,
        transactions: Sequence[ExpenseTransaction],
    ) -> Sequence[ExpenseTransaction]:
        assert all(transaction.user_id == user_id for transaction in transactions)
        for transaction in transactions:
            self.transactions[transaction.id] = transaction
        return list(transactions)

    async def get_related(
        self,
        *,
        user_id: UUID,
        transaction_id: UUID,
    ) -> Sequence[ExpenseTransaction]:
        target = self.transactions.get(transaction_id)
        if target is None or target.user_id != user_id:
            return []
        if target.installment_group_id is None:
            return [target]
        return sorted(
            (
                transaction
                for transaction in self.transactions.values()
                if transaction.user_id == user_id
                and transaction.installment_group_id == target.installment_group_id
            ),
            key=lambda transaction: transaction.installment_number or 1,
        )

    async def list_for_date(
        self,
        *,
        user_id: UUID,
        transaction_date: date,
    ) -> Sequence[ExpenseTransaction]:
        return [
            transaction
            for transaction in self.transactions.values()
            if transaction.user_id == user_id
            and transaction.transaction_date == transaction_date
        ]

    async def list_for_range(
        self,
        *,
        user_id: UUID,
        start_date: date,
        end_date: date,
    ) -> Sequence[ExpenseTransaction]:
        return [
            transaction
            for transaction in self.transactions.values()
            if transaction.user_id == user_id
            and start_date <= transaction.transaction_date <= end_date
        ]

    async def update_related(
        self,
        *,
        user_id: UUID,
        transaction_id: UUID,
        transactions: Sequence[ExpenseTransaction],
    ) -> Sequence[ExpenseTransaction]:
        if not await self.get_related(
            user_id=user_id,
            transaction_id=transaction_id,
        ):
            return []
        for transaction in transactions:
            self.transactions[transaction.id] = transaction
        return list(transactions)

    async def delete_related(
        self,
        *,
        user_id: UUID,
        transaction_id: UUID,
    ) -> Sequence[ExpenseTransaction]:
        related = await self.get_related(
            user_id=user_id,
            transaction_id=transaction_id,
        )
        for transaction in related:
            del self.transactions[transaction.id]
        return related


def test_date_and_installment_rules_are_deterministic() -> None:
    resolved = resolve_transaction_date(
        current_time=NOW,
        timezone_name="America/Argentina/Cordoba",
        requested_date=None,
    )
    amounts = split_installment_amount(Decimal("100.00"), 3)
    dates = installment_dates(date(2026, 1, 31), 3)

    assert resolved.value == date(2026, 8, 30)
    assert resolved.used_previous_day_rule
    assert amounts == [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")]
    assert dates == [date(2026, 1, 31), date(2026, 2, 1), date(2026, 3, 1)]


def test_installment_update_and_deletion_apply_to_the_group() -> None:
    async def verify() -> None:
        repository = FakeExpenseRepository()
        service = ExpenseService(repository, clock=lambda: NOW)
        user_id = uuid4()
        created = await service.add_installments(
            user_id=user_id,
            timezone_name="America/Argentina/Cordoba",
            purchase=CreateInstallmentExpense(
                expense=CreateExpense(
                    amount=Decimal("100.00"),
                    category=ExpenseCategory.HOME,
                    description="Washing machine",
                    transaction_date=date(2026, 8, 15),
                ),
                installment_count=3,
            ),
        )
        target_id = created.transactions[1].id

        updated = await service.update(
            user_id=user_id,
            timezone_name="America/Argentina/Cordoba",
            transaction_id=target_id,
            changes=ExpenseChanges(
                amount=Decimal("120.00"),
                category=ExpenseCategory.SHOPPING,
                transaction_date=date(2026, 9, 1),
            ),
        )

        assert [transaction.amount for transaction in updated] == [
            Decimal("40.00"),
            Decimal("40.00"),
            Decimal("40.00"),
        ]
        assert [transaction.transaction_date for transaction in updated] == [
            date(2026, 8, 15),
            date(2026, 9, 1),
            date(2026, 10, 1),
        ]
        assert all(
            transaction.category is ExpenseCategory.SHOPPING for transaction in updated
        )

        shifted = await service.update(
            user_id=user_id,
            timezone_name="America/Argentina/Cordoba",
            transaction_id=target_id,
            changes=ExpenseChanges(transaction_date=date(2026, 9, 6)),
        )

        assert [transaction.transaction_date for transaction in shifted] == [
            date(2026, 8, 20),
            date(2026, 9, 6),
            date(2026, 10, 6),
        ]

        deleted = await service.delete(
            user_id=user_id,
            transaction_id=target_id,
        )
        assert len(deleted) == 3
        assert not repository.transactions

    asyncio.run(verify())


def test_user_selected_future_expense_dates_are_rejected() -> None:
    async def verify() -> None:
        service = ExpenseService(FakeExpenseRepository(), clock=lambda: NOW)
        user_id = uuid4()

        try:
            await service.add_installments(
                user_id=user_id,
                timezone_name="America/Argentina/Cordoba",
                purchase=CreateInstallmentExpense(
                    expense=CreateExpense(
                        amount=Decimal("100.00"),
                        category=ExpenseCategory.HOME,
                        description="Future purchase",
                        transaction_date=date(2026, 9, 1),
                    ),
                    installment_count=3,
                ),
            )
        except ValueError as exc:
            assert str(exc) == "Expense date cannot be in the future."
        else:
            raise AssertionError("Expected a future expense date to be rejected.")

    asyncio.run(verify())


def test_refunds_subtract_from_monthly_summary() -> None:
    async def verify() -> None:
        service = ExpenseService(FakeExpenseRepository(), clock=lambda: NOW)
        user_id = uuid4()
        for amount, entry_type in (
            (Decimal("100.00"), ExpenseEntryType.EXPENSE),
            (Decimal("30.00"), ExpenseEntryType.REFUND),
        ):
            await service.add_expense(
                user_id=user_id,
                timezone_name="America/Argentina/Cordoba",
                expense=CreateExpense(
                    amount=amount,
                    category=ExpenseCategory.HOME,
                    description="Home",
                    transaction_date=date(2026, 8, 15),
                    entry_type=entry_type,
                ),
            )

        summary = await service.summarize_month(
            user_id=user_id,
            year=2026,
            month=8,
        )

        assert summary.total == Decimal("70.00")
        assert summary.category_totals[0].amount == Decimal("70.00")

    asyncio.run(verify())
