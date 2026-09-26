from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import exp
from random import random
from uuid import UUID

from harle_domain.events import (
    EventStatus,
    InteractionEvent,
    InteractionEventCandidate,
    InteractionEventRepository,
)
from harle_services.accounts import SubscriptionMaintenanceService
from harle_utils import Clock, as_utc, utc_now

MAX_USER_INACTIVITY = timedelta(days=7)
DEFAULT_INTERACTION_EVENT_LIMIT = 100


@dataclass(frozen=True, slots=True)
class InteractionEventService:
    repository: InteractionEventRepository
    clock: Clock = utc_now
    random_value: Callable[[], float] = random
    subscriptions: SubscriptionMaintenanceService | None = None

    async def get(self, *, user_id: UUID) -> InteractionEvent | None:
        return await self.repository.get_for_user(user_id=user_id)

    async def list_active(
        self,
        *,
        limit: int = DEFAULT_INTERACTION_EVENT_LIMIT,
    ) -> Sequence[InteractionEventCandidate]:
        if self.subscriptions is not None:
            await self.subscriptions.renew_due()
        current_time = self._now()
        return await self.repository.list_active(
            limit=limit,
            user_message_from=current_time - MAX_USER_INACTIVITY,
            current_time=current_time,
        )

    async def disable(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
    ) -> InteractionEvent | None:
        return await self._set_status(
            user_id=user_id,
            event_id=event_id,
            status=EventStatus.DISABLED,
        )

    async def enable(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
    ) -> InteractionEvent | None:
        return await self._set_status(
            user_id=user_id,
            event_id=event_id,
            status=EventStatus.ACTIVE,
        )

    async def _set_status(
        self,
        *,
        user_id: UUID,
        event_id: UUID,
        status: EventStatus,
    ) -> InteractionEvent | None:
        operation = (
            self.repository.enable
            if status is EventStatus.ACTIVE
            else self.repository.disable
        )
        return await operation(
            user_id=user_id,
            event_id=event_id,
            updated_at=self._now(),
        )

    async def record_user_message(
        self,
        *,
        user_id: UUID,
        update_ids: Sequence[int],
    ) -> InteractionEvent:
        event = await self.repository.record_user_message(
            user_id=user_id,
            update_ids=update_ids,
        )
        if event is None:
            raise RuntimeError("Interaction event was not provisioned.")
        return event

    async def record_agent_message(self, *, user_id: UUID) -> InteractionEvent:
        event = await self.repository.record_agent_message(
            user_id=user_id,
            occurred_at=self._now(),
        )
        if event is None:
            raise RuntimeError("Interaction event was not provisioned.")
        return event

    def should_trigger(
        self,
        event: InteractionEvent,
        *,
        scheduler_interval: timedelta,
        scale: timedelta,
    ) -> bool:
        if event.status is not EventStatus.ACTIVE:
            return False
        now = self._now()
        last_user_message = event.last_user_message_at
        latest_contact = event.latest_contact_at
        if last_user_message is None or latest_contact is None:
            return False
        user_inactivity = now - last_user_message
        contact_inactivity = now - latest_contact
        if (
            user_inactivity < timedelta(0)
            or user_inactivity > MAX_USER_INACTIVITY
            or contact_inactivity < timedelta(0)
        ):
            return False
        probability = interaction_probability(
            elapsed=contact_inactivity,
            scheduler_interval=scheduler_interval,
            scale=scale,
        )
        random_value = self.random_value()
        if not 0 <= random_value < 1:
            raise ValueError("Random value must be between zero and one.")
        return random_value < probability

    def _now(self) -> datetime:
        return as_utc(self.clock())


def interaction_probability(
    *,
    elapsed: timedelta,
    scheduler_interval: timedelta,
    scale: timedelta,
) -> float:
    if elapsed < timedelta(0):
        raise ValueError("Interaction elapsed time cannot be negative.")
    if scheduler_interval <= timedelta(0):
        raise ValueError("Scheduler interval must be positive.")
    if scale <= timedelta(0):
        raise ValueError("Interaction scale must be positive.")
    scale_seconds = scale.total_seconds()
    elapsed_ratio = elapsed.total_seconds() / scale_seconds
    next_ratio = (elapsed + scheduler_interval).total_seconds() / scale_seconds
    return 1 - exp(-(next_ratio**2 - elapsed_ratio**2))
