from asyncio import CancelledError, Lock, Task, create_task, sleep
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

from asyncpg import PostgresError

from harle_utils import MessageDeliveryError, log

from .interactions import InteractionEventService
from .notifications import EventNotificationOutcome, EventNotificationService
from .service import EventService

SCHEDULER_FAILURES = (
    MessageDeliveryError,
    OSError,
    PostgresError,
    RuntimeError,
    TypeError,
    ValueError,
)


@dataclass(slots=True)
class AgentsScheduler:
    events: EventService
    interactions: InteractionEventService
    notifications: EventNotificationService
    interval_seconds: float = 300
    sleeper: Callable[[float], Awaitable[None]] = sleep
    _task: Task[None] | None = field(default=None, init=False)
    _run_lock: Lock = field(default_factory=Lock, init=False)

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("Scheduler interval must be positive.")

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = create_task(self.run_forever())

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._task = None
        task.cancel()
        try:
            await task
        except CancelledError:
            pass

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except SCHEDULER_FAILURES as exc:
                log.warning(f"Event scheduler pass failed: {type(exc).__name__}")
            await self.sleeper(self.interval_seconds)

    async def run_once(self) -> int:
        async with self._run_lock:
            due_events = await self.events.list_due_for_notification()
            users_with_due_events = {event.user_id for event in due_events}
            delivered_count = 0
            for event in due_events:
                try:
                    log.info(
                        f"Scheduled event triggered event_id={event.id} user_id={event.user_id} "
                        f"event_type={event.event_type.value}",
                    )
                    outcome = await self.notifications.notify(event)
                    if outcome not in {
                        EventNotificationOutcome.DELIVERED,
                        EventNotificationOutcome.ALREADY_DELIVERED,
                    }:
                        continue
                    delivered_event = await self.events.mark_notification_delivered(
                        event=event,
                    )
                    if (
                        outcome is EventNotificationOutcome.DELIVERED
                        and delivered_event is not None
                    ):
                        delivered_count += 1
                except SCHEDULER_FAILURES as exc:
                    log.warning(
                        f"Event notification failed for event_id={event.id}: {type(exc).__name__}",
                    )
            interaction_candidates = await self.interactions.list_active()
            scheduler_interval = timedelta(seconds=self.interval_seconds)
            for candidate in interaction_candidates:
                interaction_event = candidate.event
                if interaction_event.user_id in users_with_due_events:
                    continue
                if not self.interactions.should_trigger(
                    interaction_event,
                    scheduler_interval=scheduler_interval,
                    scale=candidate.interaction_frequency.scale,
                ):
                    continue
                try:
                    log.info(
                        f"Scheduled event triggered event_id={interaction_event.id} user_id={interaction_event.user_id} "
                        f"event_type=interaction_event",
                    )
                    outcome = await self.notifications.notify_interaction(
                        interaction_event,
                    )
                    if outcome is EventNotificationOutcome.DELIVERED:
                        delivered_count += 1
                except SCHEDULER_FAILURES as exc:
                    log.warning(
                        f"Interaction event failed for event_id={interaction_event.id}: {type(exc).__name__}",
                    )
            return delivered_count
