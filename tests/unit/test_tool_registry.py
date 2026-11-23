import asyncio
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import cast
from uuid import UUID, uuid4

import pytest

from harle_domain.accounts import (
    ExternalIdentity,
    Plan,
    ResolvedUser,
    SubscriptionPeriod,
    SubscriptionStatus,
    User,
)
from harle_domain.events import (
    EventRepository,
    EventStatus,
    InteractionEvent,
    InternalEvent,
)
from harle_domain.expenses import ExpenseRepository
from harle_domain.profiles import (
    AssistantProfile,
    AssistantProfileRepository,
    InteractionFrequency,
)
from harle_infrastructure.google_sheets import (
    GoogleSheetsClient,
    GoogleSheetsConnectionSettings,
    LegacyGoogleSheetsSettings,
)
from harle_services.bootstrap import (
    EventToolDependencies,
    ExpenseToolDependencies,
    create_tools_injector,
)
from harle_services.events import (
    EventNotificationQuotaService,
    InteractionEventService,
    NotificationQuotaStatus,
)
from harle_services.expenses import ExpenseService
from harle_services.tools import ToolInjectionContext
from harle_services.tools.internal_events import EventIdentifierArgs, ListEventsArgs
from harle_services.tools.internal_profiles import (
    GetInteractionFrequencyArgs,
    SetInteractionFrequencyArgs,
)
from harle_utils import ToolAccessDeniedError, ToolUnavailableError

NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


class EmptyEventRepository:
    async def list_for_range(self, **_: object) -> list[InternalEvent]:
        return []

    async def disable(self, **_: object) -> InternalEvent | None:
        return None


class FakeNotificationQuotas:
    async def status(
        self,
        *,
        user_id: UUID,
        monthly_limit: int,
        period: object,
    ) -> NotificationQuotaStatus:
        del user_id, period
        return NotificationQuotaStatus(
            limit=monthly_limit,
            remaining=monthly_limit - 1,
            resets_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )


class FakeInteractions:
    async def get(self, *, user_id: UUID) -> None:
        del user_id
        return None

    async def disable(self, **_: object) -> None:
        return None

    async def enable(self, **_: object) -> None:
        return None


class FakeInteractionLifecycle:
    def __init__(self, event: InteractionEvent) -> None:
        self.event = event

    async def get(self, *, user_id: UUID) -> InteractionEvent | None:
        return self.event if self.event.user_id == user_id else None

    async def disable(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
    ) -> InteractionEvent | None:
        if (user_id, event_id) != (self.event.user_id, self.event.id):
            return None
        self.event = replace(self.event, status=EventStatus.DISABLED)
        return self.event

    async def enable(self, **_: object) -> InteractionEvent | None:
        return None


class FakeAssistantProfiles:
    def __init__(self, profile: AssistantProfile) -> None:
        self.profile = profile

    async def get(self, *, user_id: UUID) -> AssistantProfile | None:
        return self.profile if user_id == self.profile.user_id else None

    async def save(
        self,
        *,
        user_id: UUID,
        profile: AssistantProfile,
    ) -> AssistantProfile:
        if user_id != profile.user_id:
            raise ValueError
        self.profile = profile
        return profile

    async def update_interaction_frequency(
        self,
        *,
        user_id: UUID,
        frequency: InteractionFrequency,
    ) -> AssistantProfile | None:
        if user_id != self.profile.user_id:
            return None
        self.profile = replace(
            self.profile,
            interaction_frequency=frequency,
            updated_at=NOW,
        )
        return self.profile


def resolved_user(user_id: UUID) -> ResolvedUser:
    plan = Plan(
        code="basic",
        monthly_request_limit=480,
        monthly_notification_limit=60,
        active=True,
        created_at=NOW,
        updated_at=NOW,
    )
    user = User(
        id=user_id,
        display_name="Beta User",
        plan_code=plan.code,
        subscription_status=SubscriptionStatus.ACTIVE,
        subscription_valid_until=None,
        subscription_synced_at=NOW,
        subscription_period=SubscriptionPeriod(
            datetime(2026, 8, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 1, tzinfo=timezone.utc),
        ),
        created_at=NOW,
        updated_at=NOW,
    )
    identity = ExternalIdentity(
        id=uuid4(),
        user_id=user_id,
        provider="telegram",
        external_user_id="123",
        display_name=user.display_name,
        created_at=NOW,
        updated_at=NOW,
    )
    return ResolvedUser(user=user, plan=plan, identity=identity)


def test_tool_access_matrix_and_lazy_sheets_configuration() -> None:
    juan_id = uuid4()
    expense_repository = cast(ExpenseRepository, object())
    event_repository = cast(EventRepository, object())
    event_tools = EventToolDependencies(
        repository=event_repository,
        interactions=cast(InteractionEventService, FakeInteractions()),
        notification_quotas=cast(EventNotificationQuotaService, object()),
    )
    incomplete_settings = LegacyGoogleSheetsSettings(
        _env_file=None,
        LEGACY_GOOGLE_SHEETS_USER_ID=juan_id,
    )
    commercial_store = create_tools_injector(
        incomplete_settings,
        expense_tools=ExpenseToolDependencies(ExpenseService(expense_repository)),
        event_tools=event_tools,
    ).inject(
        ToolInjectionContext(
            resolved_user=resolved_user(uuid4()),
            prompt="Show my expenses",
            timezone="America/Argentina/Cordoba",
        ),
    )

    assert {tool.name for tool in commercial_store.tools} == {
        "add_expense",
        "add_refund",
        "add_installment_expense",
        "list_expenses",
        "summarize_expenses",
        "update_expense",
        "delete_expense",
    }
    assert "Google Sheets" not in commercial_store.prompt
    with pytest.raises(ToolUnavailableError):
        commercial_store.get("add_one_time_transaction")

    configured_settings = LegacyGoogleSheetsSettings(
        _env_file=None,
        LEGACY_GOOGLE_SHEETS_USER_ID=juan_id,
        EXPENSES_SPREADSHEET_ID="current",
        EXPENSES_NEXT_YEAR_SPREADSHEET_ID="next",
        GOOGLE_SERVICE_ACCOUNT_JSON_BASE64="e30=",
    )
    juan_store = create_tools_injector(
        configured_settings,
        expense_tools=ExpenseToolDependencies(ExpenseService(expense_repository)),
        event_tools=event_tools,
    ).inject(
        ToolInjectionContext(
            resolved_user=resolved_user(juan_id),
            prompt="Show my expenses",
            timezone="America/Argentina/Cordoba",
        ),
    )

    assert {tool.name for tool in juan_store.tools} == {
        "add_one_time_transaction",
        "add_in_installments_transaction",
        "get_day_expenses",
        "get_month_expenses",
        "remove_or_update_transaction",
    }
    event_store = create_tools_injector(
        configured_settings,
        expense_tools=ExpenseToolDependencies(ExpenseService(expense_repository)),
        event_tools=event_tools,
    ).inject(
        ToolInjectionContext(
            resolved_user=resolved_user(juan_id),
            prompt="Create an event tomorrow",
            timezone="America/Argentina/Cordoba",
        ),
    )
    assert {tool.name for tool in event_store.tools} == {
        "list_events",
        "create_event",
        "update_event",
        "disable_event",
        "enable_event",
        "delete_event",
    }


def test_tool_filter_uses_complete_terms_and_falls_back_to_all_families() -> None:
    store = create_tools_injector(
        LegacyGoogleSheetsSettings(_env_file=None),
        expense_tools=ExpenseToolDependencies(
            ExpenseService(cast(ExpenseRepository, object())),
        ),
        event_tools=EventToolDependencies(
            repository=cast(EventRepository, object()),
            interactions=cast(InteractionEventService, FakeInteractions()),
            notification_quotas=cast(EventNotificationQuotaService, object()),
        ),
    ).inject(
        ToolInjectionContext(
            resolved_user=resolved_user(uuid4()),
            prompt="I will eventually log lunch",
            timezone="America/Argentina/Cordoba",
        ),
    )

    names = {tool.name for tool in store.tools}
    assert "add_expense" in names
    assert "create_event" in names


def test_scheduled_tools_include_only_authorized_reads() -> None:
    injector = create_tools_injector(
        LegacyGoogleSheetsSettings(_env_file=None),
        expense_tools=ExpenseToolDependencies(
            ExpenseService(cast(ExpenseRepository, object())),
        ),
        event_tools=EventToolDependencies(
            repository=cast(EventRepository, object()),
            interactions=cast(InteractionEventService, FakeInteractions()),
            notification_quotas=cast(EventNotificationQuotaService, object()),
        ),
    )
    store = injector.inject_scheduled(
        ToolInjectionContext(
            resolved_user=resolved_user(uuid4()),
            prompt="Scheduled wake-up",
            timezone="UTC",
        ),
    )

    assert {tool.name for tool in store.tools} == {
        "list_events",
        "list_expenses",
        "summarize_expenses",
    }
    with pytest.raises(ToolUnavailableError):
        store.get("create_event")


def test_event_tools_expose_plan_notification_allowance() -> None:
    store = create_tools_injector(
        event_tools=EventToolDependencies(
            repository=cast(EventRepository, EmptyEventRepository()),
            interactions=cast(InteractionEventService, FakeInteractions()),
            notification_quotas=cast(
                EventNotificationQuotaService,
                FakeNotificationQuotas(),
            ),
        ),
    ).inject(
        ToolInjectionContext(
            resolved_user=resolved_user(uuid4()),
            prompt="List my events",
            timezone="UTC",
        ),
    )

    result = asyncio.run(
        store.get("list_events").handler(
            ListEventsArgs(
                start_date=date(2026, 8, 31),
                end_date=date(2026, 8, 31),
            ),
        ),
    )
    mutation_result = asyncio.run(
        store.get("disable_event").handler(
            EventIdentifierArgs(event_id=uuid4()),
        ),
    )

    for tool_result in (result, mutation_result):
        assert isinstance(tool_result.result, Mapping)
        assert tool_result.result["notification_quota"] == {
            "period_limit": 60,
            "remaining": 59,
            "period_ends_at": "2026-09-01T00:00:00Z",
        }


def test_interaction_event_can_be_disabled_but_not_deleted() -> None:
    user = resolved_user(uuid4())
    interaction = InteractionEvent(
        id=uuid4(),
        user_id=user.user.id,
        status=EventStatus.ACTIVE,
        last_user_message_at=NOW,
        last_agent_message_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    lifecycle = FakeInteractionLifecycle(interaction)
    store = create_tools_injector(
        event_tools=EventToolDependencies(
            repository=cast(EventRepository, EmptyEventRepository()),
            interactions=cast(InteractionEventService, lifecycle),
            notification_quotas=cast(
                EventNotificationQuotaService,
                FakeNotificationQuotas(),
            ),
        ),
    ).inject(
        ToolInjectionContext(
            resolved_user=user,
            prompt="Disable my interaction event",
            timezone="UTC",
        ),
    )

    deleted = asyncio.run(
        store.get("delete_event").handler(
            EventIdentifierArgs(event_id=interaction.id),
        ),
    )
    disabled = asyncio.run(
        store.get("disable_event").handler(
            EventIdentifierArgs(event_id=interaction.id),
        ),
    )

    assert isinstance(deleted.result, Mapping)
    assert deleted.result["ok"] is False
    assert isinstance(disabled.result, Mapping)
    assert disabled.result["ok"] is True
    assert lifecycle.event.status is EventStatus.DISABLED


def test_interaction_frequency_tools_read_update_and_exclude_scheduled_writes() -> None:
    user = resolved_user(uuid4())
    profiles = FakeAssistantProfiles(
        AssistantProfile(
            user_id=user.user.id,
            display_name="Harle",
            profile_text="Personal assistant",
            interaction_frequency=InteractionFrequency.HIGH,
            created_at=NOW,
            updated_at=NOW,
        ),
    )
    injector = create_tools_injector(
        expense_tools=ExpenseToolDependencies(
            ExpenseService(cast(ExpenseRepository, object())),
        ),
        event_tools=EventToolDependencies(
            repository=cast(EventRepository, EmptyEventRepository()),
            interactions=cast(InteractionEventService, FakeInteractions()),
            notification_quotas=cast(
                EventNotificationQuotaService,
                FakeNotificationQuotas(),
            ),
        ),
        assistant_profile_repository=cast(AssistantProfileRepository, profiles),
    )
    store = injector.inject(
        ToolInjectionContext(
            resolved_user=user,
            prompt="Set my interaction frequency to low",
            timezone="UTC",
        ),
    )

    current = asyncio.run(
        store.get("get_interaction_frequency").handler(
            GetInteractionFrequencyArgs(),
        ),
    )
    updated = asyncio.run(
        store.get("set_interaction_frequency").handler(
            SetInteractionFrequencyArgs(
                interaction_frequency=InteractionFrequency.LOW,
            ),
        ),
    )
    scheduled = injector.inject_scheduled(
        ToolInjectionContext(
            resolved_user=user,
            prompt="Scheduled wake-up",
            timezone="UTC",
        ),
    )

    assert current.result == {
        "interaction_frequency": "high",
        "scale_hours": 12,
    }
    assert updated.result == {
        "interaction_frequency": "low",
        "scale_hours": 48,
    }
    assert scheduled.get("get_interaction_frequency")
    with pytest.raises(ToolUnavailableError):
        scheduled.get("set_interaction_frequency")


def test_google_sheets_client_rechecks_uuid_before_write() -> None:
    client = GoogleSheetsClient(
        settings=GoogleSheetsConnectionSettings(
            current_year_spreadsheet_id="current",
            next_year_spreadsheet_id="next",
            service_account_json_base64="e30=",
        ),
        execution_user_id=uuid4(),
        authorized_user_id=uuid4(),
    )

    with pytest.raises(ToolAccessDeniedError):
        asyncio.run(
            client.update_formula(
                sheet_name="enero",
                cell="E2",
                formula="=10",
            ),
        )
