from collections.abc import Mapping
from datetime import timedelta
from zoneinfo import ZoneInfo

from harle_domain.events import (
    EventInterval,
    EventOccurrence,
    InternalEvent,
    MonthlyRecurrence,
    RecurrenceRule,
    WeeklyRecurrence,
)


def recurrence_payload(
    value: RecurrenceRule | None,
) -> Mapping[str, object] | None:
    if isinstance(value, WeeklyRecurrence):
        return {"week_days": sorted(day.value for day in value.days)}
    if isinstance(value, MonthlyRecurrence):
        return {"month_days": sorted(value.days)}
    return None


def event_payload(event: InternalEvent) -> Mapping[str, object]:
    schedule = _schedule_payload(event.details.interval)
    timezone_info = ZoneInfo(event.timezone)
    return {
        "event_id": str(event.id),
        "title": event.title,
        "description": event.description,
        **schedule,
        "event_type": event.event_type.value,
        "status": event.status.value,
        "recurrence_rule": recurrence_payload(event.recurrence_rule),
        "notification_window_start": event.notification_window_start.astimezone(
            timezone_info,
        ).isoformat(),
        "notify_minutes_before": int(
            (event.starts_at - event.notification_window_start).total_seconds() / 60,
        ),
        "last_notified_at": (
            event.last_notified_at.astimezone(timezone_info).isoformat()
            if event.last_notified_at is not None
            else None
        ),
    }


def occurrence_payload(occurrence: EventOccurrence) -> Mapping[str, object]:
    return {
        "event_id": str(occurrence.event_id),
        **_schedule_payload(occurrence.interval),
    }


def _schedule_payload(interval: EventInterval) -> Mapping[str, object]:
    timezone_info = ZoneInfo(interval.timezone)
    local_start = interval.starts_at.astimezone(timezone_info)
    local_end = interval.ends_at.astimezone(timezone_info)
    if interval.all_day:
        start_value = local_start.date().isoformat()
        end_value = (local_end.date() - timedelta(days=1)).isoformat()
    else:
        start_value = local_start.isoformat()
        end_value = local_end.isoformat()
    return {
        "local_start": start_value,
        "local_end": end_value,
        "timezone": interval.timezone,
        "all_day": interval.all_day,
    }
