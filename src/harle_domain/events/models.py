from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from harle_domain.profiles import InteractionFrequency


class EventStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class EventType(str, Enum):
    USER_EVENT = "user_event"
    SYSTEM_EVENT = "system_event"


class WeekDay(str, Enum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"


@dataclass(frozen=True, slots=True)
class WeeklyRecurrence:
    days: frozenset[WeekDay]

    def __post_init__(self) -> None:
        if not self.days:
            raise ValueError("Weekly recurrence requires at least one weekday.")


@dataclass(frozen=True, slots=True)
class MonthlyRecurrence:
    days: frozenset[int]

    def __post_init__(self) -> None:
        if not self.days:
            raise ValueError("Monthly recurrence requires at least one month day.")
        if not all(1 <= day <= 31 for day in self.days):
            raise ValueError("Month days must be between 1 and 31.")


RecurrenceRule = WeeklyRecurrence | MonthlyRecurrence


@dataclass(frozen=True, slots=True)
class EventInterval:
    starts_at: datetime
    ends_at: datetime
    timezone: str
    all_day: bool

    def __post_init__(self) -> None:
        try:
            timezone = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone: {self.timezone}.") from exc
        _require_utc(self.starts_at, "Event start")
        _require_utc(self.ends_at, "Event end")
        if self.ends_at <= self.starts_at:
            raise ValueError("Event end must be after its start.")
        if self.all_day:
            local_start = self.starts_at.astimezone(timezone)
            local_end = self.ends_at.astimezone(timezone)
            if local_start.time() != time.min or local_end.time() != time.min:
                raise ValueError("All-day events must use local midnight boundaries.")


@dataclass(frozen=True, slots=True)
class EventDetails:
    title: str
    description: str
    interval: EventInterval
    event_type: EventType
    status: EventStatus

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("Event title cannot be empty.")


@dataclass(frozen=True, slots=True)
class EventTimestamps:
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "Event creation time")
        _require_aware(self.updated_at, "Event update time")


@dataclass(frozen=True, slots=True)
class EventNotification:
    window_start: datetime
    last_notified_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_utc(self.window_start, "Event notification window start")
        if self.last_notified_at is not None:
            _require_utc(self.last_notified_at, "Event last notification time")


@dataclass(frozen=True, slots=True)
class EventNotificationOccurrence:
    event_id: UUID
    user_id: UUID
    starts_at: datetime
    window_start: datetime

    def __post_init__(self) -> None:
        _require_utc(self.starts_at, "Notification occurrence start")
        _require_utc(self.window_start, "Notification occurrence window start")
        if self.window_start > self.starts_at:
            raise ValueError("Notification window cannot start after its occurrence.")


@dataclass(frozen=True, slots=True)
class EventOccurrence:
    event_id: UUID
    interval: EventInterval


@dataclass(frozen=True, slots=True)
class InternalEvent:
    id: UUID
    user_id: UUID
    details: EventDetails
    notification: EventNotification
    timestamps: EventTimestamps
    recurrence_rule: RecurrenceRule | None = None

    def __post_init__(self) -> None:
        if self.notification_window_start > self.starts_at:
            raise ValueError(
                "Event notification window cannot start after the event.",
            )

    @property
    def title(self) -> str:
        return self.details.title

    @property
    def description(self) -> str:
        return self.details.description

    @property
    def starts_at(self) -> datetime:
        return self.details.interval.starts_at

    @property
    def ends_at(self) -> datetime:
        return self.details.interval.ends_at

    @property
    def timezone(self) -> str:
        return self.details.interval.timezone

    @property
    def all_day(self) -> bool:
        return self.details.interval.all_day

    @property
    def event_type(self) -> EventType:
        return self.details.event_type

    @property
    def status(self) -> EventStatus:
        return self.details.status

    @property
    def notification_window_start(self) -> datetime:
        return self.notification.window_start

    @property
    def last_notified_at(self) -> datetime | None:
        return self.notification.last_notified_at

    @property
    def created_at(self) -> datetime:
        return self.timestamps.created_at

    @property
    def updated_at(self) -> datetime:
        return self.timestamps.updated_at


@dataclass(frozen=True, slots=True)
class InteractionEvent:
    id: UUID
    user_id: UUID
    status: EventStatus
    last_user_message_at: datetime | None
    last_agent_message_at: datetime | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for value, label in (
            (self.last_user_message_at, "Last user message"),
            (self.last_agent_message_at, "Last agent message"),
        ):
            if value is not None:
                _require_utc(value, label)
        _require_utc(self.created_at, "Interaction event creation time")
        _require_utc(self.updated_at, "Interaction event update time")

    @property
    def latest_contact_at(self) -> datetime | None:
        contacts = [
            value
            for value in (self.last_user_message_at, self.last_agent_message_at)
            if value is not None
        ]
        return max(contacts) if contacts else None


@dataclass(frozen=True, slots=True)
class InteractionEventCandidate:
    event: InteractionEvent
    interaction_frequency: InteractionFrequency


def _require_utc(value: datetime, label: str) -> None:
    _require_aware(value, label)
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be stored in UTC.")


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone.")
