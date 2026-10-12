import asyncio
from collections.abc import MutableMapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from harle_domain.events import (
    EventStatus,
    EventType,
    InternalEvent,
    MonthlyRecurrence,
    NotificationTiming,
    WeekDay,
    WeeklyRecurrence,
    all_day_event_interval,
    due_recurrence_interval,
    recurrence_interval,
    recurrence_intervals_for_range,
    recurrence_overlaps_range,
    recurs_on,
    timed_event_interval,
)
from harle_services.events import (
    AllDayEventSchedule,
    CreateEvent,
    EventQuery,
    EventService,
    TimedEventSchedule,
    UpdateEvent,
)
from harle_services.tools.event_schedules import (
    CreateEventArgs,
    MonthlyRecurrenceArgs,
    UpdateEventArgs,
    WeeklyRecurrenceArgs,
)

NOW = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)


def test_recurrence_matches_weekdays_and_available_month_days() -> None:
    weekly = WeeklyRecurrence(
        frozenset({WeekDay.MONDAY, WeekDay.FRIDAY}),
    )
    monthly = MonthlyRecurrence(frozenset({15, 31}))

    assert recurs_on(weekly, date(2026, 9, 4))
    assert not recurs_on(weekly, date(2026, 9, 5))
    assert recurs_on(monthly, date(2026, 10, 31))
    assert not recurs_on(monthly, date(2026, 11, 30))


def test_recurrence_rejects_empty_or_invalid_days() -> None:
    with pytest.raises(ValueError):
        WeeklyRecurrence(frozenset())
    with pytest.raises(ValueError):
        MonthlyRecurrence(frozenset())
    with pytest.raises(ValueError):
        MonthlyRecurrence(frozenset({0, 32}))


def test_recurrence_preserves_timed_and_all_day_spans() -> None:
    timed = timed_event_interval(
        starts_at=datetime(2026, 9, 1, 23),
        ends_at=datetime(2026, 9, 2, 1),
        timezone_name="America/Argentina/Cordoba",
    )
    all_day = all_day_event_interval(
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 2),
        timezone_name="America/Argentina/Cordoba",
    )
    rule = WeeklyRecurrence(frozenset({WeekDay.FRIDAY}))

    timed_occurrence = recurrence_interval(
        interval=timed,
        rule=rule,
        local_date=date(2026, 9, 4),
    )
    all_day_occurrence = recurrence_interval(
        interval=all_day,
        rule=rule,
        local_date=date(2026, 9, 4),
    )

    assert timed_occurrence is not None
    assert timed_occurrence.ends_at - timed_occurrence.starts_at == timedelta(hours=2)
    assert all_day_occurrence is not None
    assert all_day_occurrence.ends_at - all_day_occurrence.starts_at == timedelta(
        days=2,
    )


def test_recurrence_range_and_notification_use_virtual_occurrences() -> None:
    interval = timed_event_interval(
        starts_at=datetime(2026, 9, 1, 0, 5),
        ends_at=datetime(2026, 9, 1, 1, 5),
        timezone_name="UTC",
    )
    rule = WeeklyRecurrence(frozenset({WeekDay.TUESDAY}))
    current_time = datetime(2026, 9, 7, 23, 50, tzinfo=timezone.utc)

    assert recurrence_overlaps_range(
        interval=interval,
        rule=rule,
        starts_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
        ends_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    due = due_recurrence_interval(
        interval=interval,
        rule=rule,
        timing=NotificationTiming(
            notify_before=timedelta(minutes=15),
            grace_period=timedelta(hours=1),
        ),
        last_notified_at=None,
        current_time=current_time,
    )
    assert due is not None
    assert due.starts_at == datetime(2026, 9, 8, 0, 5, tzinfo=timezone.utc)
    assert (
        due_recurrence_interval(
            interval=interval,
            rule=rule,
            timing=NotificationTiming(
                notify_before=timedelta(minutes=15),
                grace_period=timedelta(hours=1),
            ),
            last_notified_at=current_time,
            current_time=datetime(2026, 9, 8, 0, tzinfo=timezone.utc),
        )
        is None
    )
    overnight = timed_event_interval(
        starts_at=datetime(2026, 9, 7, 23),
        ends_at=datetime(2026, 9, 8, 1),
        timezone_name="UTC",
    )
    ongoing = due_recurrence_interval(
        interval=overnight,
        rule=WeeklyRecurrence(frozenset({WeekDay.MONDAY})),
        timing=NotificationTiming(
            notify_before=timedelta(0),
            grace_period=timedelta(hours=1),
        ),
        last_notified_at=None,
        current_time=datetime(2026, 9, 8, 0, 30, tzinfo=timezone.utc),
    )
    assert ongoing is not None
    assert ongoing.starts_at == datetime(2026, 9, 7, 23, tzinfo=timezone.utc)


def test_recurrence_projects_only_occurrences_overlapping_a_bounded_range() -> None:
    interval = all_day_event_interval(
        start_date=date(2026, 1, 31),
        end_date=date(2026, 1, 31),
        timezone_name="America/Argentina/Cordoba",
    )
    occurrences = recurrence_intervals_for_range(
        interval=interval,
        rule=MonthlyRecurrence(frozenset({31})),
        starts_at=datetime(2026, 2, 1, 3, tzinfo=timezone.utc),
        ends_at=datetime(2026, 4, 1, 3, tzinfo=timezone.utc),
    )

    assert [occurrence.starts_at for occurrence in occurrences] == [
        datetime(2026, 3, 31, 3, tzinfo=timezone.utc),
    ]


def test_recurring_notification_has_one_hour_grace_period() -> None:
    interval = timed_event_interval(
        starts_at=datetime(2026, 9, 1, 23, 58),
        ends_at=datetime(2026, 9, 1, 23, 59),
        timezone_name="UTC",
    )
    rule = WeeklyRecurrence(frozenset({WeekDay.TUESDAY}))
    grace_period = timedelta(hours=1)

    due = due_recurrence_interval(
        interval=interval,
        rule=rule,
        timing=NotificationTiming(
            notify_before=timedelta(0),
            grace_period=grace_period,
        ),
        last_notified_at=None,
        current_time=datetime(2026, 9, 2, 0, 30, tzinfo=timezone.utc),
    )
    expired = due_recurrence_interval(
        interval=interval,
        rule=rule,
        timing=NotificationTiming(
            notify_before=timedelta(0),
            grace_period=grace_period,
        ),
        last_notified_at=None,
        current_time=datetime(2026, 9, 2, 0, 59, tzinfo=timezone.utc),
    )

    assert due is not None
    assert due.starts_at == datetime(2026, 9, 1, 23, 58, tzinfo=timezone.utc)
    assert expired is None


class FakeEventRepository:
    def __init__(self) -> None:
        self.events: MutableMapping[UUID, InternalEvent] = {}

    async def create(
        self,
        *,
        user_id: UUID,
        event: InternalEvent,
    ) -> InternalEvent:
        assert event.user_id == user_id
        self.events[event.id] = event
        return event

    async def get(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
        include_disabled: bool = False,
    ) -> InternalEvent | None:
        event = self.events.get(event_id)
        if event is None or event.user_id != user_id:
            return None
        if event.status is EventStatus.ACTIVE:
            return event
        if include_disabled and event.status is EventStatus.DISABLED:
            return event
        return None

    async def list_for_range(
        self,
        *,
        user_id: UUID,
        starts_at: datetime,
        ends_at: datetime,
        include_disabled: bool = False,
    ) -> Sequence[InternalEvent]:
        return [
            event
            for event in self.events.values()
            if event.user_id == user_id
            and (
                event.recurrence_rule is not None
                or (event.starts_at < ends_at and event.ends_at > starts_at)
            )
            and (
                event.status is EventStatus.ACTIVE
                or (include_disabled and event.status is EventStatus.DISABLED)
            )
        ]

    async def list_due_for_notification(
        self,
        *,
        current_time: datetime,
        notification_grace_period: timedelta,
        limit: int,
    ) -> Sequence[InternalEvent]:
        due = [
            event
            for event in self.events.values()
            if event.status is EventStatus.ACTIVE
            and (
                event.recurrence_rule is not None
                or (
                    event.last_notified_at is None
                    and event.notification_window_start
                    <= current_time
                    < event.ends_at + notification_grace_period
                )
            )
        ]
        return sorted(
            due,
            key=lambda event: (
                event.notification_window_start,
                event.starts_at,
                event.id,
            ),
        )[:limit]

    async def update(
        self,
        *,
        user_id: UUID,
        event: InternalEvent,
    ) -> InternalEvent | None:
        current = await self.get(
            user_id=user_id,
            event_id=event.id,
            include_disabled=True,
        )
        if current is None:
            return None
        self.events[event.id] = event
        return event

    async def disable(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
        updated_at: datetime,
    ) -> InternalEvent | None:
        current = await self.get(user_id=user_id, event_id=event_id)
        if current is None:
            return None
        disabled = replace(
            current,
            details=replace(current.details, status=EventStatus.DISABLED),
            timestamps=replace(current.timestamps, updated_at=updated_at),
        )
        self.events[event_id] = disabled
        return disabled

    async def enable(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
        updated_at: datetime,
    ) -> InternalEvent | None:
        current = await self.get(
            user_id=user_id,
            event_id=event_id,
            include_disabled=True,
        )
        if current is None or current.status is not EventStatus.DISABLED:
            return None
        enabled = replace(
            current,
            details=replace(current.details, status=EventStatus.ACTIVE),
            timestamps=replace(current.timestamps, updated_at=updated_at),
        )
        self.events[event_id] = enabled
        return enabled

    async def mark_notification_delivered(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
        expected_updated_at: datetime,
        updated_at: datetime,
    ) -> InternalEvent | None:
        current = await self.get(user_id=user_id, event_id=event_id)
        if current is None or current.updated_at != expected_updated_at:
            return None
        delivered = replace(
            current,
            notification=replace(
                current.notification,
                last_notified_at=updated_at,
            ),
            timestamps=replace(current.timestamps, updated_at=updated_at),
        )
        self.events[event_id] = delivered
        return delivered

    async def delete(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
    ) -> InternalEvent | None:
        current = self.events.get(event_id)
        if current is None or current.user_id != user_id:
            return None
        del self.events[event_id]
        return current


def test_one_time_notification_has_one_hour_grace_period() -> None:
    async def verify() -> None:
        repository = FakeEventRepository()
        owner_id = uuid4()
        event = await EventService(
            repository,
            clock=lambda: datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
        ).create(
            user_id=owner_id,
            event=CreateEvent(
                title="Short event",
                description="",
                schedule=TimedEventSchedule(
                    starts_at=datetime(2026, 9, 1, 11),
                    ends_at=datetime(2026, 9, 1, 11, 1),
                    timezone_name="UTC",
                ),
            ),
        )
        within_grace = EventService(
            repository,
            clock=lambda: datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
        )
        expired = EventService(
            repository,
            clock=lambda: datetime(2026, 9, 1, 12, 1, tzinfo=timezone.utc),
        )

        assert [due.id for due in await within_grace.list_due_for_notification()] == [
            event.id,
        ]
        assert not await expired.list_due_for_notification()

    asyncio.run(verify())


def test_event_intervals_convert_local_and_all_day_boundaries() -> None:
    timed = timed_event_interval(
        starts_at=datetime(2026, 9, 1, 15),
        ends_at=datetime(2026, 9, 1, 16, 30),
        timezone_name="America/Argentina/Cordoba",
    )
    all_day = all_day_event_interval(
        start_date=date(2026, 3, 8),
        end_date=date(2026, 3, 8),
        timezone_name="America/New_York",
    )

    assert timed.starts_at == datetime(2026, 9, 1, 18, tzinfo=timezone.utc)
    assert timed.ends_at == datetime(2026, 9, 1, 19, 30, tzinfo=timezone.utc)
    assert all_day.ends_at - all_day.starts_at == timedelta(hours=23)
    with pytest.raises(ValueError):
        timed_event_interval(
            starts_at=datetime(2026, 9, 1, 16),
            ends_at=datetime(2026, 9, 1, 15),
            timezone_name="UTC",
        )


def test_event_service_isolates_disable_enable_and_physical_deletion() -> None:
    async def verify() -> None:
        repository = FakeEventRepository()
        service = EventService(repository, clock=lambda: NOW)
        owner_id = uuid4()
        other_id = uuid4()
        event = await service.create(
            user_id=owner_id,
            event=CreateEvent(
                title="Dentist",
                description="Routine visit",
                schedule=TimedEventSchedule(
                    starts_at=datetime(2026, 9, 1, 15),
                    ends_at=datetime(2026, 9, 1, 16),
                    timezone_name="America/Argentina/Cordoba",
                ),
            ),
        )
        assert event.event_type is EventType.USER_EVENT
        assert event.status is EventStatus.ACTIVE
        assert event.last_notified_at is None
        assert event.notification_window_start == datetime(
            2026,
            9,
            1,
            18,
            tzinfo=timezone.utc,
        )
        delivered = await service.mark_notification_delivered(event=event)
        assert delivered is not None
        assert delivered.last_notified_at == NOW

        assert (
            await service.update(
                user_id=other_id,
                event_id=event.id,
                changes=UpdateEvent(title="Changed"),
            )
            is None
        )
        updated = await service.update(
            user_id=owner_id,
            event_id=event.id,
            changes=UpdateEvent(
                title="Updated dentist",
                schedule=AllDayEventSchedule(
                    start_date=date(2026, 9, 2),
                    end_date=date(2026, 9, 2),
                    timezone_name="America/Argentina/Cordoba",
                ),
            ),
        )
        assert updated is not None
        assert updated.title == "Updated dentist"
        assert updated.all_day
        assert updated.last_notified_at is None

        disabled = await service.disable(user_id=owner_id, event_id=event.id)
        assert disabled is not None
        assert disabled.status is EventStatus.DISABLED
        updated_while_disabled = await service.update(
            user_id=owner_id,
            event_id=event.id,
            changes=UpdateEvent(title="Updated while disabled"),
        )
        assert updated_while_disabled is not None
        assert updated_while_disabled.title == "Updated while disabled"
        assert updated_while_disabled.status is EventStatus.DISABLED
        reenabled = await service.enable(user_id=owner_id, event_id=event.id)
        assert reenabled is not None
        assert reenabled.status is EventStatus.ACTIVE

        assert await service.disable(user_id=owner_id, event_id=event.id)
        assert not await service.list_for_range(
            user_id=owner_id,
            query=EventQuery(
                start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 3),
                timezone_name="America/Argentina/Cordoba",
            ),
        )
        disabled_events = await service.list_for_range(
            user_id=owner_id,
            query=EventQuery(
                start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 3),
                timezone_name="America/Argentina/Cordoba",
                include_disabled=True,
            ),
        )
        assert disabled_events
        query = EventQuery(
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 3),
            timezone_name="America/Argentina/Cordoba",
        )
        assert not service.occurrences_for_range(
            events=disabled_events,
            query=query,
        )
        assert service.occurrences_for_range(
            events=disabled_events,
            query=query,
            include_disabled=True,
        )

        deleted = await service.delete(user_id=owner_id, event_id=event.id)
        assert deleted is not None
        assert event.id not in repository.events
        assert not await service.list_for_range(
            user_id=owner_id,
            query=EventQuery(
                start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 3),
                timezone_name="America/Argentina/Cordoba",
                include_disabled=True,
            ),
        )
        assert await service.delete(user_id=other_id, event_id=event.id) is None

    asyncio.run(verify())


def test_event_service_lists_and_repeats_recurring_notifications() -> None:
    async def verify() -> None:
        repository = FakeEventRepository()
        owner_id = uuid4()
        recurrence = WeeklyRecurrence(frozenset({WeekDay.TUESDAY}))
        service = EventService(repository, clock=lambda: NOW)
        event = await service.create(
            user_id=owner_id,
            event=CreateEvent(
                title="Weekly appointment",
                description="",
                schedule=TimedEventSchedule(
                    starts_at=datetime(2026, 9, 1, 15),
                    ends_at=datetime(2026, 9, 1, 16),
                    timezone_name="UTC",
                ),
                recurrence_rule=recurrence,
            ),
        )

        october_events = await service.list_for_range(
            user_id=owner_id,
            query=EventQuery(
                start_date=date(2026, 10, 6),
                end_date=date(2026, 10, 6),
                timezone_name="UTC",
            ),
        )
        assert [listed.id for listed in october_events] == [event.id]

        first_due_service = EventService(
            repository,
            clock=lambda: datetime(2026, 9, 1, 15, 10, tzinfo=timezone.utc),
        )
        first_due = await first_due_service.list_due_for_notification()
        assert [due.starts_at for due in first_due] == [
            datetime(2026, 9, 1, 15, tzinfo=timezone.utc),
        ]
        assert await first_due_service.mark_notification_delivered(
            event=first_due[0],
        )
        assert not await first_due_service.list_due_for_notification()

        second_due_service = EventService(
            repository,
            clock=lambda: datetime(2026, 9, 8, 15, 10, tzinfo=timezone.utc),
        )
        second_due = await second_due_service.list_due_for_notification()
        assert [due.starts_at for due in second_due] == [
            datetime(2026, 9, 8, 15, tzinfo=timezone.utc),
        ]
        one_time = await service.update(
            user_id=owner_id,
            event_id=event.id,
            changes=UpdateEvent(
                recurrence_rule=None,
                replace_recurrence=True,
            ),
        )
        assert one_time is not None
        assert one_time.recurrence_rule is None

    asyncio.run(verify())


def test_event_service_preserves_notification_leads() -> None:
    async def verify() -> None:
        repository = FakeEventRepository()
        service = EventService(repository, clock=lambda: NOW)
        owner_id = uuid4()
        schedule = TimedEventSchedule(
            starts_at=datetime(2026, 9, 1, 15),
            ends_at=datetime(2026, 9, 1, 16),
            timezone_name="UTC",
        )
        defaulted = await service.create(
            user_id=owner_id,
            event=CreateEvent(
                title="Default lead",
                description="",
                schedule=schedule,
                notify_before=timedelta(0),
            ),
        )
        custom = await service.create(
            user_id=owner_id,
            event=CreateEvent(
                title="Custom lead",
                description="",
                schedule=schedule,
                notify_before=timedelta(minutes=30),
            ),
        )

        assert defaulted.starts_at == defaulted.notification_window_start
        assert defaulted.last_notified_at is None
        preserved = await service.update(
            user_id=owner_id,
            event_id=custom.id,
            changes=UpdateEvent(title="Preserved lead"),
        )
        assert preserved is not None
        assert preserved.starts_at - preserved.notification_window_start == timedelta(
            minutes=30,
        )
        reset = await service.update(
            user_id=owner_id,
            event_id=custom.id,
            changes=UpdateEvent(notify_before=timedelta(0)),
        )
        assert reset is not None
        assert reset.starts_at == reset.notification_window_start
        positive = await service.update(
            user_id=owner_id,
            event_id=custom.id,
            changes=UpdateEvent(notify_before=timedelta(minutes=45)),
        )
        assert positive is not None
        assert positive.starts_at - positive.notification_window_start == timedelta(
            minutes=45,
        )

    asyncio.run(verify())


def test_event_tool_preserves_omitted_zero_and_positive_notification_leads() -> None:
    schedule = {
        "title": "Reminder",
        "starts_at": datetime(2026, 9, 1, 15),
        "ends_at": datetime(2026, 9, 1, 16),
    }

    assert CreateEventArgs(**schedule).notify_minutes_before == 0
    assert (
        CreateEventArgs(**schedule, notify_minutes_before=0).notify_minutes_before == 0
    )
    assert (
        CreateEventArgs(**schedule, notify_minutes_before=30).notify_minutes_before
        == 30
    )
    with pytest.raises(ValueError):
        CreateEventArgs(**schedule, notify_minutes_before=525601)

    event_id = uuid4()
    assert (
        UpdateEventArgs(event_id=event_id, title="Keep").notify_minutes_before is None
    )
    assert (
        UpdateEventArgs(
            event_id=event_id,
            notify_minutes_before=0,
        ).notify_minutes_before
        == 0
    )
    assert (
        UpdateEventArgs(
            event_id=event_id,
            notify_minutes_before=30,
        ).notify_minutes_before
        == 30
    )


def test_event_tool_validates_symmetric_recurrence_rules() -> None:
    schedule = {
        "title": "Reminder",
        "starts_at": datetime(2026, 9, 1, 15),
        "ends_at": datetime(2026, 9, 1, 16),
    }
    weekly = CreateEventArgs(
        **schedule,
        recurrence_rule={"week_days": ["monday", "friday"]},
    )
    monthly = CreateEventArgs(
        **schedule,
        recurrence_rule={"month_days": [1, 15, 31]},
    )

    assert isinstance(weekly.recurrence_rule, WeeklyRecurrenceArgs)
    assert isinstance(monthly.recurrence_rule, MonthlyRecurrenceArgs)
    assert not UpdateEventArgs(event_id=uuid4(), title="Keep").replaces_recurrence
    assert UpdateEventArgs(
        event_id=uuid4(),
        recurrence_rule=None,
    ).replaces_recurrence
    with pytest.raises(ValueError):
        CreateEventArgs(**schedule, recurrence_rule={"month_days": [0]})
