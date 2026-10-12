from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

from harle_domain.events import (
    EventDetails,
    EventInterval,
    EventNotification,
    EventOccurrence,
    EventRepository,
    EventStatus,
    EventTimestamps,
    EventType,
    InternalEvent,
    NotificationTiming,
    RecurrenceRule,
    all_day_event_interval,
    due_recurrence_interval,
    event_range,
    recurrence_intervals_for_range,
    recurrence_overlaps_range,
    timed_event_interval,
)

DEFAULT_NOTIFICATION_LEAD = timedelta(0)
DEFAULT_NOTIFICATION_GRACE_PERIOD = timedelta(hours=1)
DEFAULT_DUE_EVENT_LIMIT = 100


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class TimedEventSchedule:
    starts_at: datetime
    ends_at: datetime
    timezone_name: str

    def to_interval(self) -> EventInterval:
        return timed_event_interval(
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            timezone_name=self.timezone_name,
        )


@dataclass(frozen=True, slots=True)
class AllDayEventSchedule:
    start_date: date
    end_date: date
    timezone_name: str

    def to_interval(self) -> EventInterval:
        return all_day_event_interval(
            start_date=self.start_date,
            end_date=self.end_date,
            timezone_name=self.timezone_name,
        )


EventSchedule = TimedEventSchedule | AllDayEventSchedule


@dataclass(frozen=True, slots=True)
class CreateEvent:
    title: str
    description: str
    schedule: EventSchedule
    event_type: EventType = EventType.USER_EVENT
    notify_before: timedelta = DEFAULT_NOTIFICATION_LEAD
    recurrence_rule: RecurrenceRule | None = None

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("Event title cannot be empty.")
        _require_notification_lead(self.notify_before)


@dataclass(frozen=True, slots=True)
class UpdateEvent:
    title: str | None = None
    description: str | None = None
    schedule: EventSchedule | None = None
    event_type: EventType | None = None
    notify_before: timedelta | None = None
    recurrence_rule: RecurrenceRule | None = None
    replace_recurrence: bool = False

    def __post_init__(self) -> None:
        changes = (
            self.title,
            self.description,
            self.schedule,
            self.event_type,
            self.notify_before,
            self.replace_recurrence,
        )
        if changes == (None, None, None, None, None, False):
            raise ValueError("At least one event change is required.")
        if self.title is not None and not self.title.strip():
            raise ValueError("Event title cannot be empty.")
        if self.notify_before is not None:
            _require_notification_lead(self.notify_before)


@dataclass(frozen=True, slots=True)
class EventQuery:
    start_date: date
    end_date: date
    timezone_name: str
    include_disabled: bool = False


@dataclass(frozen=True, slots=True)
class EventService:
    repository: EventRepository
    clock: Callable[[], datetime] = utc_now

    async def list_for_range(
        self,
        *,
        user_id: UUID,
        query: EventQuery,
    ) -> Sequence[InternalEvent]:
        bounded_range = event_range(
            start_date=query.start_date,
            end_date=query.end_date,
            timezone_name=query.timezone_name,
        )
        events = await self.repository.list_for_range(
            user_id=user_id,
            starts_at=bounded_range.starts_at,
            ends_at=bounded_range.ends_at,
            include_disabled=query.include_disabled,
        )
        return [
            event
            for event in events
            if event.recurrence_rule is None
            or recurrence_overlaps_range(
                interval=event.details.interval,
                rule=event.recurrence_rule,
                starts_at=bounded_range.starts_at,
                ends_at=bounded_range.ends_at,
            )
        ]

    async def get(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
        include_disabled: bool = False,
    ) -> InternalEvent | None:
        return await self.repository.get(
            user_id=user_id,
            event_id=event_id,
            include_disabled=include_disabled,
        )

    def occurrences_for_range(
        self,
        *,
        events: Sequence[InternalEvent],
        query: EventQuery,
    ) -> Sequence[EventOccurrence]:
        bounded_range = event_range(
            start_date=query.start_date,
            end_date=query.end_date,
            timezone_name=query.timezone_name,
        )
        occurrences: list[EventOccurrence] = []
        for event in events:
            if event.status is not EventStatus.ACTIVE:
                continue
            if event.recurrence_rule is None:
                if (
                    event.starts_at < bounded_range.ends_at
                    and event.ends_at > bounded_range.starts_at
                ):
                    occurrences.append(
                        EventOccurrence(
                            event_id=event.id,
                            interval=event.details.interval,
                        ),
                    )
                continue
            occurrences.extend(
                EventOccurrence(event_id=event.id, interval=interval)
                for interval in recurrence_intervals_for_range(
                    interval=event.details.interval,
                    rule=event.recurrence_rule,
                    starts_at=bounded_range.starts_at,
                    ends_at=bounded_range.ends_at,
                )
            )
        return sorted(
            occurrences,
            key=lambda occurrence: (
                occurrence.interval.starts_at,
                occurrence.interval.ends_at,
                occurrence.event_id,
            ),
        )

    async def create(
        self,
        *,
        user_id: UUID,
        event: CreateEvent,
    ) -> InternalEvent:
        now = self._now()
        interval = event.schedule.to_interval()
        notification_lead = event.notify_before
        created = InternalEvent(
            id=uuid4(),
            user_id=user_id,
            details=EventDetails(
                title=event.title.strip(),
                description=event.description.strip(),
                interval=interval,
                event_type=event.event_type,
                status=EventStatus.ACTIVE,
            ),
            notification=EventNotification(
                window_start=interval.starts_at - notification_lead,
            ),
            timestamps=EventTimestamps(
                created_at=now,
                updated_at=now,
            ),
            recurrence_rule=event.recurrence_rule,
        )
        return await self.repository.create(user_id=user_id, event=created)

    async def update(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
        changes: UpdateEvent,
    ) -> InternalEvent | None:
        current = await self.repository.get(
            user_id=user_id,
            event_id=event_id,
        )
        if current is None:
            return None
        interval = (
            changes.schedule.to_interval()
            if changes.schedule is not None
            else current.details.interval
        )
        notification_lead = (
            changes.notify_before
            if changes.notify_before is not None
            else current.starts_at - current.notification_window_start
        )
        notification_window_start = interval.starts_at - notification_lead
        recurrence_rule = (
            changes.recurrence_rule
            if changes.replace_recurrence
            else current.recurrence_rule
        )
        notification_changed = (
            interval.starts_at != current.starts_at
            or notification_window_start != current.notification_window_start
            or recurrence_rule != current.recurrence_rule
        )
        updated = replace(
            current,
            details=replace(
                current.details,
                title=(
                    changes.title.strip()
                    if changes.title is not None
                    else current.title
                ),
                description=(
                    changes.description.strip()
                    if changes.description is not None
                    else current.description
                ),
                interval=interval,
                event_type=changes.event_type or current.event_type,
            ),
            notification=replace(
                current.notification,
                window_start=notification_window_start,
                last_notified_at=(
                    None
                    if notification_changed
                    else current.notification.last_notified_at
                ),
            ),
            timestamps=replace(current.timestamps, updated_at=self._now()),
            recurrence_rule=recurrence_rule,
        )
        return await self.repository.update(user_id=user_id, event=updated)

    async def list_due_for_notification(
        self,
        *,
        limit: int = DEFAULT_DUE_EVENT_LIMIT,
    ) -> Sequence[InternalEvent]:
        if limit <= 0:
            raise ValueError("Due event limit must be positive.")
        current_time = self._now()
        candidates = await self.repository.list_due_for_notification(
            current_time=current_time,
            notification_grace_period=DEFAULT_NOTIFICATION_GRACE_PERIOD,
            limit=limit,
        )
        due_events: list[InternalEvent] = []
        for event in candidates:
            if event.recurrence_rule is None:
                due_events.append(event)
                continue
            due_event = self._due_recurring_event(event, current_time)
            if due_event is not None:
                due_events.append(due_event)
        return sorted(
            due_events,
            key=lambda event: (
                event.notification_window_start,
                event.starts_at,
                event.id,
            ),
        )[:limit]

    async def mark_notification_delivered(
        self,
        *,
        event: InternalEvent,
    ) -> InternalEvent | None:
        return await self.repository.mark_notification_delivered(
            user_id=event.user_id,
            event_id=event.id,
            expected_updated_at=event.updated_at,
            updated_at=self._now(),
        )

    async def disable(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
    ) -> InternalEvent | None:
        return await self.repository.disable(
            user_id=user_id,
            event_id=event_id,
            updated_at=self._now(),
        )

    async def enable(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
    ) -> InternalEvent | None:
        return await self.repository.enable(
            user_id=user_id,
            event_id=event_id,
            updated_at=self._now(),
        )

    async def delete(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
    ) -> InternalEvent | None:
        return await self.repository.delete(
            user_id=user_id,
            event_id=event_id,
        )

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Event service clock must return a timezone-aware time.")
        return now.astimezone(timezone.utc)

    def _due_recurring_event(
        self,
        event: InternalEvent,
        current_time: datetime,
    ) -> InternalEvent | None:
        recurrence_rule = event.recurrence_rule
        if recurrence_rule is None:
            return None
        notification_lead = event.starts_at - event.notification_window_start
        occurrence = due_recurrence_interval(
            interval=event.details.interval,
            rule=recurrence_rule,
            timing=NotificationTiming(
                notify_before=notification_lead,
                grace_period=DEFAULT_NOTIFICATION_GRACE_PERIOD,
            ),
            last_notified_at=event.last_notified_at,
            current_time=current_time,
        )
        if occurrence is None:
            return None
        return replace(
            event,
            details=replace(event.details, interval=occurrence),
            notification=replace(
                event.notification,
                window_start=occurrence.starts_at - notification_lead,
            ),
        )


def _require_notification_lead(value: timedelta) -> None:
    if value < timedelta(0):
        raise ValueError("Event notification lead cannot be negative.")
