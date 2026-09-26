from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar
from uuid import UUID, uuid4

import asyncpg

from harle_domain.accounts import (
    AccountOverview,
    ExternalIdentity,
    FreeAccountPeriod,
    GoogleRegistration,
    SubscriptionPeriod,
    SubscriptionStatus,
)
from harle_domain.profiles import AssistantProfile, UserProfile
from harle_infrastructure.postgres.repositories.accounts import (
    insert_external_identity,
)
from harle_infrastructure.postgres.repositories.profiles import (
    save_assistant_profile,
    save_user_profile,
)

FieldT = TypeVar("FieldT")
DEFAULT_ASSISTANT_NAME = "Harle"
DEFAULT_ASSISTANT_PROFILE = "A concise and transparent AI personal assistant."


@dataclass(frozen=True, slots=True)
class PostgresWebAccountRepository:
    pool: asyncpg.Pool

    async def find_or_create_google_user(
        self,
        *,
        registration: GoogleRegistration,
    ) -> UUID:
        identity = registration.identity
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"google:{identity.subject}",
                )
                existing = await connection.fetchval(
                    """
                    SELECT user_id
                    FROM external_identities
                    WHERE provider = 'google'
                        AND external_user_id = $1
                    """,
                    identity.subject,
                )
                if isinstance(existing, UUID):
                    await connection.execute(
                        """
                        UPDATE external_identities
                        SET email = $2,
                            display_name = $3,
                            updated_at = $4
                        WHERE provider = 'google'
                            AND external_user_id = $1
                        """,
                        identity.subject,
                        identity.email,
                        identity.display_name,
                        registration.created_at,
                    )
                    return existing
                user_id = await connection.fetchval(
                    """
                    INSERT INTO users (
                        name,
                        display_name,
                        plan_code,
                        subscription_status,
                        subscription_valid_until,
                        subscription_synced_at,
                        subscription_period_starts_at,
                        subscription_period_ends_at,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        $1, $1, 'free', 'active', NULL, $2, $3, $4, $2, $2
                    )
                    RETURNING id
                    """,
                    identity.display_name,
                    registration.created_at,
                    registration.period.starts_at,
                    registration.period.ends_at,
                )
                if not isinstance(user_id, UUID):
                    raise RuntimeError("Could not create the Google user.")
                await insert_external_identity(
                    connection,
                    identity=ExternalIdentity(
                        id=uuid4(),
                        user_id=user_id,
                        provider="google",
                        external_user_id=identity.subject,
                        display_name=identity.display_name,
                        created_at=registration.created_at,
                        updated_at=registration.created_at,
                    ),
                )
                await connection.execute(
                    """
                    UPDATE external_identities
                    SET email = $2
                    WHERE provider = 'google'
                        AND external_user_id = $1
                    """,
                    identity.subject,
                    identity.email,
                )
                await save_user_profile(
                    connection,
                    UserProfile(
                        user_id=user_id,
                        preferred_name=identity.display_name,
                        locale=registration.locale,
                        timezone=registration.timezone,
                        latitude=None,
                        longitude=None,
                        personal_history="",
                        created_at=registration.created_at,
                        updated_at=registration.created_at,
                    ),
                )
                await save_assistant_profile(
                    connection,
                    AssistantProfile(
                        user_id=user_id,
                        display_name=DEFAULT_ASSISTANT_NAME,
                        profile_text=DEFAULT_ASSISTANT_PROFILE,
                        created_at=registration.created_at,
                        updated_at=registration.created_at,
                    ),
                )
                return user_id

    async def get_overview(self, *, user_id: UUID) -> AccountOverview | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    users.id,
                    users.display_name,
                    users.plan_code,
                    users.subscription_status,
                    users.subscription_period_starts_at,
                    users.subscription_period_ends_at,
                    EXISTS (
                        SELECT 1
                        FROM external_identities
                        WHERE user_id = users.id
                            AND provider = 'telegram'
                    ) AS telegram_linked
                FROM users
                WHERE users.id = $1
                """,
                user_id,
            )
        return _account_overview(row) if row is not None else None

    async def get_free_period(
        self,
        *,
        user_id: UUID,
        current_time: datetime,
    ) -> FreeAccountPeriod | None:
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                    id,
                    subscription_period_starts_at,
                    subscription_period_ends_at
                FROM users
                WHERE id = $1
                    AND plan_code = 'free'
                    AND subscription_status = 'active'
                    AND (
                        subscription_valid_until IS NULL
                        OR subscription_valid_until > $2
                    )
                """,
                user_id,
                current_time,
            )
        if row is None:
            return None
        return FreeAccountPeriod(
            user_id=_required(row, "id", UUID),
            period=_subscription_period(row),
        )

    async def list_due_free_periods(
        self,
        *,
        current_time: datetime,
        limit: int,
    ) -> list[FreeAccountPeriod]:
        if limit <= 0:
            raise ValueError("Free-period renewal limit must be positive.")
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    id,
                    subscription_period_starts_at,
                    subscription_period_ends_at
                FROM users
                WHERE plan_code = 'free'
                    AND subscription_status = 'active'
                    AND (
                        subscription_valid_until IS NULL
                        OR subscription_valid_until > $1
                    )
                    AND subscription_period_ends_at <= $1
                ORDER BY subscription_period_ends_at, id
                LIMIT $2
                """,
                current_time,
                limit,
            )
        return [
            FreeAccountPeriod(
                user_id=_required(row, "id", UUID),
                period=_subscription_period(row),
            )
            for row in rows
        ]

    async def replace_free_period(
        self,
        *,
        user_id: UUID,
        expected_period: SubscriptionPeriod,
        new_period: SubscriptionPeriod,
        synchronized_at: datetime,
    ) -> bool:
        async with self.pool.acquire() as connection:
            result = await connection.execute(
                """
                UPDATE users
                SET subscription_period_starts_at = $4,
                    subscription_period_ends_at = $5,
                    subscription_synced_at = $6,
                    updated_at = $6
                WHERE id = $1
                    AND plan_code = 'free'
                    AND subscription_status = 'active'
                    AND subscription_period_starts_at = $2
                    AND subscription_period_ends_at = $3
                """,
                user_id,
                expected_period.starts_at,
                expected_period.ends_at,
                new_period.starts_at,
                new_period.ends_at,
                synchronized_at,
            )
        return result == "UPDATE 1"


def _account_overview(row: asyncpg.Record) -> AccountOverview:
    return AccountOverview(
        user_id=_required(row, "id", UUID),
        display_name=_required(row, "display_name", str),
        plan_code=_required(row, "plan_code", str),
        subscription_status=SubscriptionStatus(
            _required(row, "subscription_status", str),
        ),
        subscription_period=_subscription_period(row),
        telegram_linked=_required(row, "telegram_linked", bool),
    )


def _subscription_period(row: asyncpg.Record) -> SubscriptionPeriod:
    return SubscriptionPeriod(
        starts_at=_required(row, "subscription_period_starts_at", datetime),
        ends_at=_required(row, "subscription_period_ends_at", datetime),
    )


def _required(
    row: asyncpg.Record,
    key: str,
    expected: type[FieldT],
) -> FieldT:
    value: object = row[key]
    if not isinstance(value, expected):
        raise TypeError(f"Unexpected {key} value.")
    return value
