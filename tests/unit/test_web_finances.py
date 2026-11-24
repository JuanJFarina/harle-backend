from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import harle_api.routes.finances as finance_routes
from harle_api.exception_handlers import (
    register_exception_handlers,
    register_request_id_middleware,
)
from harle_api.routes.finances import router as finances_router
from harle_api.settings import ApiSettings
from harle_domain.accounts import BrowserSession
from harle_domain.expenses import (
    BudgetAllocation,
    BudgetCategory,
    FinanceBudgetSettings,
    IncomeHistoryEntry,
    MonthlyFinanceAnalysis,
)
from harle_utils import AuthenticationRequiredError, InvalidCsrfError

NOW = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
USER_ID = UUID("10000000-0000-0000-0000-000000000001")


class FakeSessions:
    async def resolve(self, token: str | None) -> BrowserSession:
        if token != "session-token":
            raise AuthenticationRequiredError
        return BrowserSession(uuid4(), USER_ID, NOW + timedelta(days=30), NOW)

    def require_csrf(self, *, session_token: str, csrf_token: str | None) -> None:
        if session_token != "session-token" or csrf_token != "csrf-token":
            raise InvalidCsrfError


class FakeFinances:
    def __init__(self) -> None:
        self.income = IncomeHistoryEntry(
            id=uuid4(),
            user_id=USER_ID,
            amount=Decimal("1000.00"),
            currency="ARS",
            effective_on=date(2026, 11, 1),
            created_at=NOW,
            updated_at=NOW,
        )
        self.budget = FinanceBudgetSettings(
            user_id=USER_ID,
            allocations=tuple(
                BudgetAllocation(
                    category,
                    Decimal("100.00")
                    if category is BudgetCategory.SAVINGS
                    else Decimal("0.00"),
                )
                for category in BudgetCategory
            ),
            created_at=NOW,
            updated_at=NOW,
        )

    async def analyze_month(self, **values: object) -> MonthlyFinanceAnalysis:
        if values["trend_days"] not in (30, 90, 365):
            raise ValueError("Unsupported trend period.")
        return MonthlyFinanceAnalysis(
            year=2026,
            month=11,
            income=self.income,
            budget=self.budget,
            gross_expenses=Decimal("0.00"),
            gross_refunds=Decimal("0.00"),
            net_expenses=Decimal("0.00"),
            remaining_income=Decimal("1000.00"),
            savings_target=Decimal("1000.00"),
            categories=(),
            trend=(),
            transactions=(),
        )

    async def income_history(self, **_: object) -> list[IncomeHistoryEntry]:
        return [self.income]

    async def save_income(
        self,
        *,
        amount: Decimal,
        effective_on: date,
        **_: object,
    ) -> IncomeHistoryEntry:
        self.income = IncomeHistoryEntry(
            id=uuid4(),
            user_id=USER_ID,
            amount=amount,
            currency="ARS",
            effective_on=effective_on,
            created_at=NOW,
            updated_at=NOW,
        )
        return self.income

    async def save_budget(
        self,
        *,
        percentages: dict[BudgetCategory, Decimal],
        **_: object,
    ) -> FinanceBudgetSettings:
        self.budget = FinanceBudgetSettings(
            user_id=USER_ID,
            allocations=tuple(
                BudgetAllocation(category, percentages[category])
                for category in BudgetCategory
            ),
            created_at=NOW,
            updated_at=NOW,
        )
        return self.budget


def test_finance_dashboard_requires_session_and_returns_configuration(
    monkeypatch,
) -> None:
    client = _client(monkeypatch)

    unauthorized = client.get("/api/finances/dashboard?year=2026&month=11")
    client.cookies.set("harle_session", "session-token")
    response = client.get("/api/finances/dashboard?year=2026&month=11")

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    payload = response.json()
    assert payload["income"]["amount"] == "1000.00"
    assert payload["budget"]["allocations"][-1] == {
        "category": "savings",
        "percentage": "100.00",
    }


def test_finance_dashboard_accepts_only_supported_trend_periods(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.cookies.set("harle_session", "session-token")

    supported = client.get(
        "/api/finances/dashboard?year=2026&month=11&trend_days=90",
    )
    unsupported = client.get(
        "/api/finances/dashboard?year=2026&month=11&trend_days=60",
    )

    assert supported.status_code == 200
    assert unsupported.status_code == 422


def test_finance_mutations_require_origin_csrf_and_complete_budget(
    monkeypatch,
) -> None:
    client = _client(monkeypatch)
    client.cookies.set("harle_session", "session-token")
    income = {"amount": "1200.00", "effective_on": "2026-11-20"}

    rejected = client.post(
        "/api/finances/income",
        json=income,
    )
    saved = client.post(
        "/api/finances/income",
        json=income,
        headers={"Origin": "https://app.test", "X-CSRF-Token": "csrf-token"},
    )
    invalid_budget = client.post(
        "/api/finances/budget",
        json={
            "rent": 10,
            "essential_services": 0,
            "non_essential_services": 0,
            "home": 0,
            "transport": 0,
            "outings": 0,
            "shopping": 0,
            "other": 0,
            "savings": 10,
        },
        headers={"Origin": "https://app.test", "X-CSRF-Token": "csrf-token"},
    )

    assert rejected.status_code == 403
    assert saved.status_code == 200
    assert saved.json()["income"]["amount"] == "1200.00"
    assert invalid_budget.status_code == 422
    assert invalid_budget.json()["error"]["code"] == "validation_error"


def test_web_expense_creation_requires_explicit_date(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.cookies.set("harle_session", "session-token")

    response = client.post(
        "/api/finances/expenses",
        json={
            "amount": "100.00",
            "category": "home",
            "description": "Home supplies",
        },
        headers={"Origin": "https://app.test", "X-CSRF-Token": "csrf-token"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_legacy_google_sheets_user_cannot_access_internal_finances(
    monkeypatch,
) -> None:
    client = _client(monkeypatch, legacy_user_id=USER_ID)
    client.cookies.set("harle_session", "session-token")

    response = client.get("/api/finances/dashboard?year=2026&month=11")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "access_denied"


def _client(monkeypatch, legacy_user_id: UUID | None = None) -> TestClient:
    finances = FakeFinances()
    runtime = SimpleNamespace(
        finances=finances,
        expenses=SimpleNamespace(),
        tools=SimpleNamespace(
            access_policy=SimpleNamespace(
                legacy_google_sheets_user_id=legacy_user_id,
            ),
        ),
        profiles=SimpleNamespace(
            get=_async_result(SimpleNamespace(timezone="America/Argentina/Cordoba")),
        ),
    )
    accounts = SimpleNamespace(sessions=FakeSessions())
    monkeypatch.setattr(finance_routes, "get_settings", _settings)
    monkeypatch.setattr(finance_routes, "get_runtime", lambda _: runtime)
    monkeypatch.setattr(finance_routes, "get_account_runtime", lambda _: accounts)
    app = FastAPI()
    register_exception_handlers(app)
    register_request_id_middleware(app)
    app.include_router(finances_router)
    return TestClient(app, raise_server_exceptions=False)


def _settings() -> ApiSettings:
    return ApiSettings.model_construct(
        SESSION_COOKIE_NAME="harle_session",
        FRONTEND_ORIGINS=["https://app.test"],
    )


def _async_result(value: object):
    async def result(**_: object) -> object:
        return value

    return result
