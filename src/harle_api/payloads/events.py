from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harle_services.tools.event_schedules import (
    CreateEventArgs,
    MonthlyRecurrenceArgs,
    RecurrenceArgs,
    UpdateEventArgs,
    WeeklyRecurrenceArgs,
)


class CreateEventPayload(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    start_date: date | None = None
    end_date: date | None = None
    timezone: str | None = Field(default=None, min_length=1)
    notify_minutes_before: int = Field(default=0, ge=0)
    recurrence_rule: RecurrenceArgs | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_event(self) -> "CreateEventPayload":
        self.to_args()
        return self

    def to_args(self) -> CreateEventArgs:
        return CreateEventArgs.model_validate(self.model_dump())


class UpdateEventPayload(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    start_date: date | None = None
    end_date: date | None = None
    timezone: str | None = Field(default=None, min_length=1)
    notify_minutes_before: int | None = Field(default=None, ge=0)
    recurrence_rule: RecurrenceArgs | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_event(self) -> "UpdateEventPayload":
        self.to_args(UUID(int=0))
        return self

    def to_args(self, event_id: UUID) -> UpdateEventArgs:
        return UpdateEventArgs.model_validate(
            {
                "event_id": event_id,
                **self.model_dump(exclude_unset=True),
            },
        )


class WeeklyRecurrencePayload(WeeklyRecurrenceArgs):
    pass


class MonthlyRecurrencePayload(MonthlyRecurrenceArgs):
    pass


EventRecurrencePayload = WeeklyRecurrencePayload | MonthlyRecurrencePayload


class EventPayload(BaseModel):
    event_id: UUID
    title: str
    description: str
    local_start: str
    local_end: str
    timezone: str
    all_day: bool
    event_type: str
    status: str
    recurrence_rule: EventRecurrencePayload | None
    notification_window_start: datetime
    notify_minutes_before: int
    last_notified_at: datetime | None


class EventOccurrencePayload(BaseModel):
    event_id: UUID
    local_start: str
    local_end: str
    timezone: str
    all_day: bool


class NotificationQuotaPayload(BaseModel):
    period_limit: int
    remaining: int
    period_ends_at: datetime


class EventListPayload(BaseModel):
    start_date: date
    end_date: date
    timezone: str
    events: list[EventPayload]
    occurrences: list[EventOccurrencePayload]
    notification_quota: NotificationQuotaPayload


class EventMutationPayload(BaseModel):
    operation: str
    event: EventPayload
    notification_quota: NotificationQuotaPayload
