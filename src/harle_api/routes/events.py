from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request

from harle_api.dependencies import get_account_runtime, get_runtime
from harle_api.payloads import (
    CreateEventPayload,
    EventListPayload,
    EventMutationPayload,
    UpdateEventPayload,
)
from harle_api.settings import ApiSettings, get_settings
from harle_domain.accounts import SubscriptionPeriod
from harle_domain.events import EventStatus, EventType, InternalEvent
from harle_services.events import (
    CreateEvent,
    EventQuery,
    UpdateEvent,
    event_payload,
    occurrence_payload,
)
from harle_services.tools.event_schedules import (
    notification_lead,
    optional_schedule,
    recurrence_rule,
    schedule,
)
from harle_utils import (
    EventNotFoundError,
    InvalidCsrfError,
    InvalidEventError,
    MissingProfileError,
)

router = APIRouter(prefix="/api/events")
MAX_EVENT_RANGE_DAYS = 62


@dataclass(frozen=True, slots=True)
class WebEventContext:
    user_id: UUID
    timezone: str
    notification_limit: int
    subscription_period: SubscriptionPeriod


@router.get("", response_model=EventListPayload)
async def get_events(
    request: Request,
    start_date: Annotated[date, Query()],
    end_date: Annotated[date, Query()],
    include_disabled: bool = True,
) -> EventListPayload:
    context = await _context(request)
    query = _event_query(
        start_date=start_date,
        end_date=end_date,
        timezone_name=context.timezone,
        include_disabled=include_disabled,
    )
    runtime = get_runtime(request)
    events = [
        event
        for event in await runtime.scheduler.events.list_for_range(
            user_id=context.user_id,
            query=query,
        )
        if event.event_type is EventType.USER_EVENT
    ]
    occurrences = runtime.scheduler.events.occurrences_for_range(
        events=events,
        query=query,
        include_disabled=True,
    )
    return EventListPayload.model_validate(
        {
            "start_date": start_date,
            "end_date": end_date,
            "timezone": context.timezone,
            "events": [event_payload(event) for event in events],
            "occurrences": [
                occurrence_payload(occurrence) for occurrence in occurrences
            ],
            "notification_quota": await _quota_payload(request, context),
        },
    )


@router.post("", response_model=EventMutationPayload)
async def post_event(
    payload: CreateEventPayload,
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> EventMutationPayload:
    context = await _mutation_context(request, x_csrf_token)
    validated = payload.to_args()
    try:
        event = await get_runtime(request).scheduler.events.create(
            user_id=context.user_id,
            event=CreateEvent(
                title=validated.title,
                description=validated.description,
                schedule=schedule(validated, context.timezone),
                event_type=EventType.USER_EVENT,
                notify_before=timedelta(
                    minutes=validated.notify_minutes_before,
                ),
                recurrence_rule=recurrence_rule(validated.recurrence_rule),
            ),
        )
    except (OverflowError, ValueError) as exc:
        raise InvalidEventError from exc
    return await _mutation_payload(request, context, event, "created")


@router.post("/{event_id}", response_model=EventMutationPayload)
async def post_event_update(
    event_id: UUID,
    payload: UpdateEventPayload,
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> EventMutationPayload:
    context = await _mutation_context(request, x_csrf_token)
    await _require_user_event(request, context, event_id)
    validated = payload.to_args(event_id)
    try:
        event = await get_runtime(request).scheduler.events.update(
            user_id=context.user_id,
            event_id=event_id,
            changes=UpdateEvent(
                title=validated.title,
                description=validated.description,
                schedule=optional_schedule(validated, context.timezone),
                notify_before=notification_lead(
                    validated.notify_minutes_before,
                ),
                recurrence_rule=recurrence_rule(validated.recurrence_rule),
                replace_recurrence=validated.replaces_recurrence,
            ),
        )
    except (OverflowError, ValueError) as exc:
        raise InvalidEventError from exc
    if event is None:
        raise EventNotFoundError
    return await _mutation_payload(request, context, event, "updated")


@router.post("/{event_id}/disable", response_model=EventMutationPayload)
async def post_event_disable(
    event_id: UUID,
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> EventMutationPayload:
    context = await _mutation_context(request, x_csrf_token)
    current = await _require_user_event(request, context, event_id)
    if current.status is EventStatus.DISABLED:
        return await _mutation_payload(request, context, current, "disabled")
    event = await get_runtime(request).scheduler.events.disable(
        user_id=context.user_id,
        event_id=event_id,
    )
    if event is None:
        raise EventNotFoundError
    return await _mutation_payload(request, context, event, "disabled")


@router.post("/{event_id}/enable", response_model=EventMutationPayload)
async def post_event_enable(
    event_id: UUID,
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> EventMutationPayload:
    context = await _mutation_context(request, x_csrf_token)
    event = await _require_user_event(request, context, event_id)
    if event.status is EventStatus.ACTIVE:
        return await _mutation_payload(request, context, event, "enabled")
    enabled = await get_runtime(request).scheduler.events.enable(
        user_id=context.user_id,
        event_id=event_id,
    )
    if enabled is None:
        raise EventNotFoundError
    return await _mutation_payload(request, context, enabled, "enabled")


@router.post("/{event_id}/delete", response_model=EventMutationPayload)
async def post_event_delete(
    event_id: UUID,
    request: Request,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> EventMutationPayload:
    context = await _mutation_context(request, x_csrf_token)
    await _require_user_event(request, context, event_id)
    event = await get_runtime(request).scheduler.events.delete(
        user_id=context.user_id,
        event_id=event_id,
    )
    if event is None:
        raise EventNotFoundError
    return await _mutation_payload(request, context, event, "deleted")


async def _context(request: Request) -> WebEventContext:
    settings = get_settings()
    account_runtime = get_account_runtime(request)
    session = await account_runtime.sessions.resolve(
        request.cookies.get(settings.SESSION_COOKIE_NAME),
    )
    profile = await get_runtime(request).profiles.get(user_id=session.user_id)
    if profile is None:
        raise MissingProfileError
    subscription = await account_runtime.paid_subscriptions.overview(
        user_id=session.user_id,
    )
    return WebEventContext(
        user_id=session.user_id,
        timezone=profile.timezone,
        notification_limit=subscription.plan.notification_limit,
        subscription_period=subscription.period,
    )


async def _mutation_context(
    request: Request,
    csrf_token: str | None,
) -> WebEventContext:
    settings = get_settings()
    _require_frontend_origin(request, settings)
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    account_runtime = get_account_runtime(request)
    await account_runtime.sessions.resolve(token)
    account_runtime.sessions.require_csrf(
        session_token=token or "",
        csrf_token=csrf_token,
    )
    return await _context(request)


async def _require_user_event(
    request: Request,
    context: WebEventContext,
    event_id: UUID,
) -> InternalEvent:
    event = await get_runtime(request).scheduler.events.get(
        user_id=context.user_id,
        event_id=event_id,
        include_disabled=True,
    )
    if event is None or event.event_type is not EventType.USER_EVENT:
        raise EventNotFoundError
    return event


async def _quota_payload(
    request: Request,
    context: WebEventContext,
) -> dict[str, object]:
    status = await get_runtime(request).scheduler.notifications.quotas.status(
        user_id=context.user_id,
        monthly_limit=context.notification_limit,
        period=context.subscription_period,
    )
    return {
        "period_limit": status.limit,
        "remaining": status.remaining,
        "period_ends_at": status.resets_at,
    }


async def _mutation_payload(
    request: Request,
    context: WebEventContext,
    event: InternalEvent,
    operation: str,
) -> EventMutationPayload:
    return EventMutationPayload.model_validate(
        {
            "operation": operation,
            "event": event_payload(event),
            "notification_quota": await _quota_payload(request, context),
        },
    )


def _event_query(
    *,
    start_date: date,
    end_date: date,
    timezone_name: str,
    include_disabled: bool,
) -> EventQuery:
    if end_date < start_date or (end_date - start_date).days > MAX_EVENT_RANGE_DAYS:
        raise InvalidEventError
    return EventQuery(
        start_date=start_date,
        end_date=end_date,
        timezone_name=timezone_name,
        include_disabled=include_disabled,
    )


def _require_frontend_origin(request: Request, settings: ApiSettings) -> None:
    origin = request.headers.get("origin")
    if origin is None or origin not in settings.FRONTEND_ORIGINS:
        raise InvalidCsrfError
