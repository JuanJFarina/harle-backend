from datetime import date, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harle_domain.events import (
    EventType,
    MonthlyRecurrence,
    RecurrenceRule,
    WeekDay,
    WeeklyRecurrence,
)
from harle_services.events import (
    MAX_NOTIFICATION_LEAD_MINUTES,
    AllDayEventSchedule,
    EventSchedule,
    TimedEventSchedule,
)

ScheduleKind = Literal["timed", "all_day"]


class WeeklyRecurrenceArgs(BaseModel):
    week_days: set[WeekDay] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")


MonthDay = Annotated[int, Field(ge=1, le=31)]


class MonthlyRecurrenceArgs(BaseModel):
    month_days: set[MonthDay] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid")


RecurrenceArgs = WeeklyRecurrenceArgs | MonthlyRecurrenceArgs


class CreateEventArgs(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    start_date: date | None = None
    end_date: date | None = None
    timezone: str | None = Field(default=None, min_length=1)
    event_type: EventType = EventType.USER_EVENT
    notify_minutes_before: int = Field(
        default=0,
        ge=0,
        le=MAX_NOTIFICATION_LEAD_MINUTES,
    )
    recurrence_rule: RecurrenceArgs | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_schedule(self) -> "CreateEventArgs":
        _schedule_kind(self, required=True)
        return self


class UpdateEventArgs(BaseModel):
    event_id: UUID
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    start_date: date | None = None
    end_date: date | None = None
    timezone: str | None = Field(default=None, min_length=1)
    event_type: EventType | None = None
    notify_minutes_before: int | None = Field(
        default=None,
        ge=0,
        le=MAX_NOTIFICATION_LEAD_MINUTES,
    )
    recurrence_rule: RecurrenceArgs | None = None

    model_config = ConfigDict(extra="forbid")

    @property
    def replaces_recurrence(self) -> bool:
        return "recurrence_rule" in self.model_fields_set

    @model_validator(mode="after")
    def validate_changes(self) -> "UpdateEventArgs":
        schedule_kind = _schedule_kind(self, required=False)
        changes = (
            self.title,
            self.description,
            schedule_kind,
            self.event_type,
            self.notify_minutes_before,
            self.replaces_recurrence,
        )
        if changes == (None, None, None, None, None, False):
            raise ValueError("At least one event change is required.")
        return self


def schedule(
    args: CreateEventArgs | UpdateEventArgs,
    default_timezone: str,
) -> EventSchedule:
    event_schedule = optional_schedule(args, default_timezone)
    if event_schedule is None:
        raise ValueError("A complete event schedule is required.")
    return event_schedule


def optional_schedule(
    args: CreateEventArgs | UpdateEventArgs,
    default_timezone: str,
) -> EventSchedule | None:
    kind = _schedule_kind(args, required=False)
    if kind is None:
        return None
    timezone_name = args.timezone or default_timezone
    if kind == "timed":
        assert args.starts_at is not None
        assert args.ends_at is not None
        return TimedEventSchedule(
            starts_at=args.starts_at,
            ends_at=args.ends_at,
            timezone_name=timezone_name,
        )
    assert args.start_date is not None
    assert args.end_date is not None
    return AllDayEventSchedule(
        start_date=args.start_date,
        end_date=args.end_date,
        timezone_name=timezone_name,
    )


def recurrence_rule(value: RecurrenceArgs | None) -> RecurrenceRule | None:
    if isinstance(value, WeeklyRecurrenceArgs):
        return WeeklyRecurrence(frozenset(value.week_days))
    if isinstance(value, MonthlyRecurrenceArgs):
        return MonthlyRecurrence(frozenset(value.month_days))
    return None


def notification_lead(minutes: int | None) -> timedelta | None:
    return timedelta(minutes=minutes) if minutes is not None else None


def _schedule_kind(
    args: CreateEventArgs | UpdateEventArgs,
    *,
    required: bool,
) -> ScheduleKind | None:
    timed_values = (args.starts_at, args.ends_at)
    all_day_values = (args.start_date, args.end_date)
    has_timed = any(value is not None for value in timed_values)
    has_all_day = any(value is not None for value in all_day_values)
    if has_timed and has_all_day:
        raise ValueError("Use either a timed or an all-day event schedule.")
    if has_timed:
        if any(value is None for value in timed_values):
            raise ValueError("Timed events require both starts_at and ends_at.")
        return "timed"
    if has_all_day:
        if any(value is None for value in all_day_values):
            raise ValueError("All-day events require both start_date and end_date.")
        return "all_day"
    if args.timezone is not None:
        raise ValueError("A timezone can only be supplied with an event schedule.")
    if required:
        raise ValueError("A timed or all-day event schedule is required.")
    return None
