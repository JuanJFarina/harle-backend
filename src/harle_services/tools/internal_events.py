from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from harle_domain.events import (
    InteractionEvent,
    InternalEvent,
)
from harle_domain.tools import (
    ToolCallResult,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolFamily,
    ToolHandler,
)
from harle_services.events import (
    CreateEvent,
    EventNotificationQuotaService,
    EventQuery,
    EventService,
    InteractionEventService,
    UpdateEvent,
    event_payload,
)

from .event_schedules import (
    CreateEventArgs,
    UpdateEventArgs,
    notification_lead,
    optional_schedule,
    recurrence_rule,
    schedule,
)
from .registry import ToolFamilyRegistration, ToolHandlerFactory

ModelT = TypeVar("ModelT", bound=BaseModel)


class ListEventsArgs(BaseModel):
    start_date: date
    end_date: date
    include_disabled: bool = False

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_range(self) -> "ListEventsArgs":
        if self.end_date < self.start_date:
            raise ValueError("Event range end date cannot precede its start date.")
        return self


class EventIdentifierArgs(BaseModel):
    event_id: UUID

    model_config = ConfigDict(extra="forbid")


FAMILY = ToolFamily.INTERNAL_EVENTS

SHARED_INSTRUCTIONS = """For every internal event tool:
- Timed event starts_at and ends_at are local ISO date-times without UTC offsets. A supplied IANA timezone overrides the user's profile timezone.
- All-day events use start_date and inclusive end_date. Use the same date for a one-day event.
- Use exactly one complete timed or all-day schedule. Updating a schedule may also change between timed and all-day.
- Use user_event for the user's agenda and system_event for an internal reminder or task for the assistant.
- notify_minutes_before defaults to 0 on creation, meaning notification at event start. On update, omission preserves the current lead.
- recurrence_rule is either {"week_days": [...]} for infinite weekly recurrence or {"month_days": [...]} for infinite monthly recurrence. Omit it for a one-time event.
- On update, omit recurrence_rule to preserve it or send null to make the event one-time.
- Recurring events remain one event definition. A missing month day produces no occurrence, and every update changes the complete definition.
- Normal reads hide disabled events; include them only when explicitly requested. Disabling is reversible and deletion is permanent.
- Each user also owns one interaction_event with no schedule. It can be disabled or re-enabled but cannot be updated or deleted.
- Event results include the user's separate subscription-period notification limit, remaining allowance, and period end.
- Events never create external calendar work."""

DEFINITIONS = (
    ToolDefinition(
        name="list_events",
        family=FAMILY,
        description=(
            "List events overlapping an inclusive local date range. Disabled "
            "events are optional."
        ),
        argument_model=ListEventsArgs,
        effect=ToolEffect.READ,
        can_run_concurrently=True,
    ),
    ToolDefinition(
        name="create_event",
        family=FAMILY,
        description="Create a one-time or recurring timed or all-day event.",
        argument_model=CreateEventArgs,
        effect=ToolEffect.MODIFY,
        can_run_concurrently=False,
    ),
    ToolDefinition(
        name="update_event",
        family=FAMILY,
        description=(
            "Partially update an owned scheduled event by UUID, optionally "
            "replacing its complete schedule."
        ),
        argument_model=UpdateEventArgs,
        effect=ToolEffect.MODIFY,
        can_run_concurrently=False,
    ),
    ToolDefinition(
        name="disable_event",
        family=FAMILY,
        description="Disable an owned active event by UUID.",
        argument_model=EventIdentifierArgs,
        effect=ToolEffect.MODIFY,
        can_run_concurrently=False,
    ),
    ToolDefinition(
        name="enable_event",
        family=FAMILY,
        description="Re-enable an owned disabled event by UUID.",
        argument_model=EventIdentifierArgs,
        effect=ToolEffect.MODIFY,
        can_run_concurrently=False,
    ),
    ToolDefinition(
        name="delete_event",
        family=FAMILY,
        description="Permanently delete an owned active or disabled event by UUID.",
        argument_model=EventIdentifierArgs,
        effect=ToolEffect.MODIFY,
        can_run_concurrently=False,
    ),
)


def create_internal_events_registration(
    service: EventService,
    interactions: InteractionEventService,
    notification_quotas: EventNotificationQuotaService,
) -> ToolFamilyRegistration:
    def build_handlers(
        context: ToolExecutionContext,
    ) -> Mapping[str, ToolHandler]:
        return _EventToolHandlers(
            service,
            interactions,
            notification_quotas,
            context,
        ).mapping()

    return _event_registration(build_handlers)


@dataclass(frozen=True, slots=True)
class _EventToolHandlers:
    service: EventService
    interactions: InteractionEventService
    notification_quotas: EventNotificationQuotaService
    context: ToolExecutionContext

    def mapping(self) -> Mapping[str, ToolHandler]:
        return {
            "list_events": self.list_events,
            "create_event": self.create_event,
            "update_event": self.update_event,
            "disable_event": self.disable_event,
            "enable_event": self.enable_event,
            "delete_event": self.delete_event,
        }

    async def list_events(self, args: BaseModel) -> ToolCallResult:
        self.context.require_family(FAMILY)
        validated = _require_model(args, ListEventsArgs)
        notification_quota = await self._notification_quota()
        interaction_event = await self.interactions.get(
            user_id=self.context.user_id,
        )
        events = await self.service.list_for_range(
            user_id=self.context.user_id,
            query=EventQuery(
                start_date=validated.start_date,
                end_date=validated.end_date,
                timezone_name=self.context.timezone,
                include_disabled=validated.include_disabled,
            ),
        )
        return ToolCallResult(
            called_tool_name="list_events",
            result={
                "ok": True,
                "start_date": validated.start_date.isoformat(),
                "end_date": validated.end_date.isoformat(),
                "events": [event_payload(event) for event in events],
                "interaction_event": (
                    _interaction_event_payload(interaction_event)
                    if interaction_event is not None
                    else None
                ),
                "notification_quota": notification_quota,
            },
        )

    async def create_event(self, args: BaseModel) -> ToolCallResult:
        self.context.require_family(FAMILY)
        validated = _require_model(args, CreateEventArgs)
        notification_quota = await self._notification_quota()
        event = await self.service.create(
            user_id=self.context.user_id,
            event=CreateEvent(
                title=validated.title,
                description=validated.description,
                schedule=schedule(validated, self.context.timezone),
                event_type=validated.event_type,
                notify_before=timedelta(
                    minutes=validated.notify_minutes_before,
                ),
                recurrence_rule=recurrence_rule(validated.recurrence_rule),
            ),
        )
        return ToolCallResult(
            called_tool_name="create_event",
            result=_mutation_payload(event, "created", notification_quota),
        )

    async def update_event(self, args: BaseModel) -> ToolCallResult:
        self.context.require_family(FAMILY)
        validated = _require_model(args, UpdateEventArgs)
        notification_quota = await self._notification_quota()
        interaction_event = await self.interactions.get(
            user_id=self.context.user_id,
        )
        if interaction_event is not None and interaction_event.id == validated.event_id:
            return ToolCallResult(
                called_tool_name="update_event",
                result=_protected_interaction_payload(
                    interaction_event,
                    "updated",
                    notification_quota,
                ),
            )
        event = await self.service.update(
            user_id=self.context.user_id,
            event_id=validated.event_id,
            changes=UpdateEvent(
                title=validated.title,
                description=validated.description,
                schedule=optional_schedule(validated, self.context.timezone),
                event_type=validated.event_type,
                notify_before=notification_lead(
                    validated.notify_minutes_before,
                ),
                recurrence_rule=recurrence_rule(validated.recurrence_rule),
                replace_recurrence=validated.replaces_recurrence,
            ),
        )
        return ToolCallResult(
            called_tool_name="update_event",
            result=_mutation_payload(event, "updated", notification_quota),
        )

    async def disable_event(self, args: BaseModel) -> ToolCallResult:
        self.context.require_family(FAMILY)
        validated = _require_model(args, EventIdentifierArgs)
        notification_quota = await self._notification_quota()
        event = await self.service.disable(
            user_id=self.context.user_id,
            event_id=validated.event_id,
        )
        interaction_event = None
        if event is None:
            interaction_event = await self.interactions.disable(
                user_id=self.context.user_id,
                event_id=validated.event_id,
            )
        return ToolCallResult(
            called_tool_name="disable_event",
            result=(
                _interaction_mutation_payload(
                    interaction_event,
                    "disabled",
                    notification_quota,
                )
                if interaction_event is not None
                else _mutation_payload(event, "disabled", notification_quota)
            ),
        )

    async def enable_event(self, args: BaseModel) -> ToolCallResult:
        self.context.require_family(FAMILY)
        validated = _require_model(args, EventIdentifierArgs)
        notification_quota = await self._notification_quota()
        event = await self.service.enable(
            user_id=self.context.user_id,
            event_id=validated.event_id,
        )
        interaction_event = None
        if event is None:
            interaction_event = await self.interactions.enable(
                user_id=self.context.user_id,
                event_id=validated.event_id,
            )
        return ToolCallResult(
            called_tool_name="enable_event",
            result=(
                _interaction_mutation_payload(
                    interaction_event,
                    "enabled",
                    notification_quota,
                )
                if interaction_event is not None
                else _mutation_payload(event, "enabled", notification_quota)
            ),
        )

    async def delete_event(self, args: BaseModel) -> ToolCallResult:
        self.context.require_family(FAMILY)
        validated = _require_model(args, EventIdentifierArgs)
        notification_quota = await self._notification_quota()
        interaction_event = await self.interactions.get(
            user_id=self.context.user_id,
        )
        if interaction_event is not None and interaction_event.id == validated.event_id:
            return ToolCallResult(
                called_tool_name="delete_event",
                result=_protected_interaction_payload(
                    interaction_event,
                    "deleted",
                    notification_quota,
                ),
            )
        event = await self.service.delete(
            user_id=self.context.user_id,
            event_id=validated.event_id,
        )
        return ToolCallResult(
            called_tool_name="delete_event",
            result=_mutation_payload(event, "deleted", notification_quota),
        )

    async def _notification_quota(self) -> Mapping[str, object]:
        return await _notification_quota_payload(
            self.notification_quotas,
            self.context,
        )


def _event_registration(
    handler_factory: ToolHandlerFactory,
) -> ToolFamilyRegistration:
    return ToolFamilyRegistration(
        FAMILY,
        SHARED_INSTRUCTIONS,
        DEFINITIONS,
        handler_factory,
    )


def _mutation_payload(
    event: InternalEvent | None,
    operation: str,
    notification_quota: Mapping[str, object],
) -> Mapping[str, object]:
    return {
        "ok": event is not None,
        "operation": operation,
        "reason": None if event is not None else "Event was not found.",
        "event": event_payload(event) if event is not None else None,
        "notification_quota": notification_quota,
    }


def _interaction_mutation_payload(
    event: InteractionEvent | None,
    operation: str,
    notification_quota: Mapping[str, object],
) -> Mapping[str, object]:
    return {
        "ok": event is not None,
        "operation": operation,
        "reason": None if event is not None else "Event was not found.",
        "event": (_interaction_event_payload(event) if event is not None else None),
        "notification_quota": notification_quota,
    }


def _protected_interaction_payload(
    event: InteractionEvent,
    operation: str,
    notification_quota: Mapping[str, object],
) -> Mapping[str, object]:
    return {
        "ok": False,
        "operation": operation,
        "reason": "Interaction events can only be disabled or re-enabled.",
        "event": _interaction_event_payload(event),
        "notification_quota": notification_quota,
    }


async def _notification_quota_payload(
    service: EventNotificationQuotaService,
    context: ToolExecutionContext,
) -> Mapping[str, object]:
    period = context.subscription_period
    if period is None:
        raise ValueError("Subscription period is required for event tools.")
    status = await service.status(
        user_id=context.user_id,
        monthly_limit=context.monthly_notification_limit,
        period=period,
    )
    return {
        "period_limit": status.limit,
        "remaining": status.remaining,
        "period_ends_at": status.resets_at.isoformat().replace("+00:00", "Z"),
    }


def _interaction_event_payload(
    event: InteractionEvent,
) -> Mapping[str, object]:
    return {
        "event_id": str(event.id),
        "event_type": "interaction_event",
        "status": event.status.value,
        "last_user_message_at": (
            event.last_user_message_at.isoformat()
            if event.last_user_message_at is not None
            else None
        ),
        "last_agent_message_at": (
            event.last_agent_message_at.isoformat()
            if event.last_agent_message_at is not None
            else None
        ),
    }


def _require_model(value: BaseModel, expected: type[ModelT]) -> ModelT:
    if not isinstance(value, expected):
        raise TypeError("Validated tool arguments do not match the tool definition.")
    return value
