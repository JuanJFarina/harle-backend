from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import (
    EventInterval,
    MonthlyRecurrence,
    RecurrenceRule,
    WeekDay,
    WeeklyRecurrence,
)

WEEK_DAYS = (
    WeekDay.MONDAY,
    WeekDay.TUESDAY,
    WeekDay.WEDNESDAY,
    WeekDay.THURSDAY,
    WeekDay.FRIDAY,
    WeekDay.SATURDAY,
    WeekDay.SUNDAY,
)


@dataclass(frozen=True, slots=True)
class EventRange:
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        if self.ends_at <= self.starts_at:
            raise ValueError("Event range end must be on or after its start date.")


@dataclass(frozen=True, slots=True)
class NotificationTiming:
    notify_before: timedelta
    grace_period: timedelta

    def __post_init__(self) -> None:
        if self.notify_before < timedelta(0):
            raise ValueError("Event notification lead cannot be negative.")
        if self.grace_period < timedelta(0):
            raise ValueError("Event notification grace period cannot be negative.")


def recurs_on(rule: RecurrenceRule, local_date: date) -> bool:
    if isinstance(rule, WeeklyRecurrence):
        return WEEK_DAYS[local_date.weekday()] in rule.days
    if isinstance(rule, MonthlyRecurrence):
        return local_date.day in rule.days
    raise TypeError("Unknown recurrence rule.")


def recurrence_interval(
    *,
    interval: EventInterval,
    rule: RecurrenceRule,
    local_date: date,
) -> EventInterval | None:
    timezone_info = _timezone(interval.timezone)
    anchor_start = interval.starts_at.astimezone(timezone_info)
    if local_date < anchor_start.date() or not recurs_on(rule, local_date):
        return None
    anchor_end = interval.ends_at.astimezone(timezone_info)
    day_span = (anchor_end.date() - anchor_start.date()).days
    if interval.all_day:
        return all_day_event_interval(
            start_date=local_date,
            end_date=local_date + timedelta(days=day_span - 1),
            timezone_name=interval.timezone,
        )
    return timed_event_interval(
        starts_at=datetime.combine(local_date, anchor_start.time()),
        ends_at=datetime.combine(
            local_date + timedelta(days=day_span),
            anchor_end.time(),
        ),
        timezone_name=interval.timezone,
    )


def recurrence_overlaps_range(
    *,
    interval: EventInterval,
    rule: RecurrenceRule,
    starts_at: datetime,
    ends_at: datetime,
) -> bool:
    if ends_at <= starts_at:
        raise ValueError("Event range end must be after its start.")
    timezone_info = _timezone(interval.timezone)
    anchor_start = interval.starts_at.astimezone(timezone_info)
    anchor_end = interval.ends_at.astimezone(timezone_info)
    day_span = (anchor_end.date() - anchor_start.date()).days
    local_date = max(
        anchor_start.date(),
        starts_at.astimezone(timezone_info).date() - timedelta(days=day_span),
    )
    last_date = ends_at.astimezone(timezone_info).date()
    while local_date <= last_date:
        occurrence = recurrence_interval(
            interval=interval,
            rule=rule,
            local_date=local_date,
        )
        if (
            occurrence is not None
            and occurrence.starts_at < ends_at
            and occurrence.ends_at > starts_at
        ):
            return True
        local_date += timedelta(days=1)
    return False


def recurrence_intervals_for_range(
    *,
    interval: EventInterval,
    rule: RecurrenceRule,
    starts_at: datetime,
    ends_at: datetime,
) -> tuple[EventInterval, ...]:
    if ends_at <= starts_at:
        raise ValueError("Event range end must be after its start.")
    timezone_info = _timezone(interval.timezone)
    anchor_start = interval.starts_at.astimezone(timezone_info)
    anchor_end = interval.ends_at.astimezone(timezone_info)
    day_span = (anchor_end.date() - anchor_start.date()).days
    local_date = max(
        anchor_start.date(),
        starts_at.astimezone(timezone_info).date() - timedelta(days=day_span),
    )
    last_date = ends_at.astimezone(timezone_info).date()
    occurrences: list[EventInterval] = []
    while local_date <= last_date:
        occurrence = recurrence_interval(
            interval=interval,
            rule=rule,
            local_date=local_date,
        )
        if (
            occurrence is not None
            and occurrence.starts_at < ends_at
            and occurrence.ends_at > starts_at
        ):
            occurrences.append(occurrence)
        local_date += timedelta(days=1)
    return tuple(occurrences)


def due_recurrence_interval(
    *,
    interval: EventInterval,
    rule: RecurrenceRule,
    timing: NotificationTiming,
    last_notified_at: datetime | None,
    current_time: datetime,
) -> EventInterval | None:
    _require_aware(current_time, "Current time")
    if last_notified_at is not None:
        _require_aware(last_notified_at, "Last notification time")
    timezone_info = _timezone(interval.timezone)
    anchor_start = interval.starts_at.astimezone(timezone_info)
    day_span = (
        interval.ends_at.astimezone(timezone_info).date() - anchor_start.date()
    ).days
    local_date = max(
        anchor_start.date(),
        (current_time - timing.grace_period).astimezone(timezone_info).date()
        - timedelta(days=day_span),
    )
    last_date = (current_time + timing.notify_before).astimezone(timezone_info).date()
    while local_date <= last_date:
        occurrence = recurrence_interval(
            interval=interval,
            rule=rule,
            local_date=local_date,
        )
        if occurrence is not None:
            window_start = occurrence.starts_at - timing.notify_before
            not_already_notified = (
                last_notified_at is None or last_notified_at < window_start
            )
            if window_start <= current_time < occurrence.ends_at + timing.grace_period:
                if not_already_notified:
                    return occurrence
        local_date += timedelta(days=1)
    return None


def timed_event_interval(
    *,
    starts_at: datetime,
    ends_at: datetime,
    timezone_name: str,
) -> EventInterval:
    timezone_info = _timezone(timezone_name)
    return EventInterval(
        starts_at=_local_datetime_to_utc(starts_at, timezone_info),
        ends_at=_local_datetime_to_utc(ends_at, timezone_info),
        timezone=timezone_name,
        all_day=False,
    )


def all_day_event_interval(
    *,
    start_date: date,
    end_date: date,
    timezone_name: str,
) -> EventInterval:
    if end_date < start_date:
        raise ValueError("All-day event end date cannot precede its start date.")
    timezone_info = _timezone(timezone_name)
    return EventInterval(
        starts_at=_local_midnight(start_date, timezone_info),
        ends_at=_local_midnight(_following_day(end_date), timezone_info),
        timezone=timezone_name,
        all_day=True,
    )


def event_range(
    *,
    start_date: date,
    end_date: date,
    timezone_name: str,
) -> EventRange:
    if end_date < start_date:
        raise ValueError("Event range end date cannot precede its start date.")
    timezone_info = _timezone(timezone_name)
    return EventRange(
        starts_at=_local_midnight(start_date, timezone_info),
        ends_at=_local_midnight(_following_day(end_date), timezone_info),
    )


def _local_datetime_to_utc(
    value: datetime,
    timezone_info: ZoneInfo,
) -> datetime:
    if value.tzinfo is not None or value.utcoffset() is not None:
        raise ValueError("Event date-times must be local values without UTC offsets.")
    candidates = (
        value.replace(tzinfo=timezone_info, fold=0),
        value.replace(tzinfo=timezone_info, fold=1),
    )
    valid_utc_values = {
        candidate.astimezone(timezone.utc)
        for candidate in candidates
        if candidate.astimezone(timezone.utc)
        .astimezone(timezone_info)
        .replace(tzinfo=None)
        == value
    }
    if not valid_utc_values:
        raise ValueError("Event date-time does not exist in the selected timezone.")
    if len(valid_utc_values) > 1:
        raise ValueError("Event date-time is ambiguous in the selected timezone.")
    return valid_utc_values.pop()


def _local_midnight(value: date, timezone_info: ZoneInfo) -> datetime:
    return datetime.combine(value, time.min, timezone_info).astimezone(timezone.utc)


def _following_day(value: date) -> date:
    try:
        return value + timedelta(days=1)
    except OverflowError as exc:
        raise ValueError("Event date exceeds the supported range.") from exc


def _timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {timezone_name}.") from exc


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone.")
