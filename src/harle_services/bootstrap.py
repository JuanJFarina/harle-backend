from dataclasses import dataclass
from uuid import UUID

import asyncpg

from harle_domain.conversations.ports import ConversationStore
from harle_domain.events import EventRepository
from harle_domain.expenses import ExpenseRepository
from harle_domain.messaging import (
    OutboundMessenger,
    RecentMediaStore,
    TelegramMediaDownloader,
)
from harle_domain.profiles import AssistantProfileRepository
from harle_infrastructure.google_identity import GoogleOAuthClient
from harle_infrastructure.google_sheets import (
    GoogleSheetsClientFactory,
    LegacyGoogleSheetsSettings,
)
from harle_infrastructure.mercado_pago import MercadoPagoClient
from harle_infrastructure.postgres import (
    PostgresAccountRepository,
    PostgresAssistantProfileRepository,
    PostgresBrowserSessionRepository,
    PostgresConversationRepository,
    PostgresConversationStore,
    PostgresEventNotificationUsageRepository,
    PostgresEventRepository,
    PostgresExpenseRepository,
    PostgresInteractionEventRepository,
    PostgresPaymentSubscriptionRepository,
    PostgresTelegramLinkRepository,
    PostgresTelegramUpdateRepository,
    PostgresUserProfileRepository,
    PostgresWebAccountRepository,
    create_postgres_pool,
    validate_postgres_schema,
)
from harle_infrastructure.telegram import InMemoryRecentMediaStore, TelegramMessenger
from harle_services.access import PreflightService
from harle_services.accounts import (
    FreeSubscriptionService,
    GoogleAuthService,
    PaidSubscriptionService,
    SessionService,
    SubscriptionMaintenanceService,
    TelegramLinkCommandService,
    TelegramLinkService,
    WebAccountService,
)
from harle_services.events import (
    AgentsScheduler,
    EventNotificationQuotaService,
    EventNotificationService,
    EventService,
    InteractionEventService,
)
from harle_services.expenses import ExpenseService
from harle_services.messaging import MessageCoordinator
from harle_services.profiles import AssistantProfileService
from harle_services.runtime import UserRuntimeFactory
from harle_services.tools import (
    ToolAccessPolicy,
    ToolFamilyRegistration,
    ToolInjectionContext,
    ToolRegistry,
    ToolsInjector,
    create_internal_events_registration,
    create_internal_expenses_registration,
    create_internal_profiles_registration,
    create_legacy_google_sheets_registration,
    create_recent_media_registration,
)


@dataclass(frozen=True, slots=True)
class AccountRuntime:
    google_auth: GoogleAuthService
    sessions: SessionService
    accounts: WebAccountService
    telegram_links: TelegramLinkService
    telegram_link_commands: TelegramLinkCommandService
    paid_subscriptions: PaidSubscriptionService
    subscriptions: SubscriptionMaintenanceService


@dataclass(frozen=True, slots=True)
class _AccountAccessRuntime:
    account: AccountRuntime
    preflight: PreflightService


@dataclass(frozen=True, slots=True)
class TelegramRuntime:
    messenger: OutboundMessenger
    media_downloader: TelegramMediaDownloader
    recent_media: RecentMediaStore
    maximum_media_request_size: int
    account: AccountRuntime | None = None


@dataclass(frozen=True, slots=True)
class ProcessRuntime:
    pool: asyncpg.Pool
    preflight: PreflightService
    users: UserRuntimeFactory
    tools: ToolsInjector
    messages: MessageCoordinator
    telegram: TelegramRuntime
    scheduler: AgentsScheduler

    @property
    def messenger(self) -> OutboundMessenger:
        return self.telegram.messenger

    @property
    def media_downloader(self) -> TelegramMediaDownloader:
        return self.telegram.media_downloader

    @property
    def recent_media(self) -> RecentMediaStore:
        return self.telegram.recent_media

    @property
    def maximum_media_request_size(self) -> int:
        return self.telegram.maximum_media_request_size

    @property
    def interactions(self) -> InteractionEventService:
        return self.scheduler.interactions

    @property
    def account(self) -> AccountRuntime | None:
        return self.telegram.account


@dataclass(frozen=True, slots=True)
class AccountRuntimeConfig:
    telegram_bot_username: str
    google_oauth_client_id: str
    google_oauth_client_secret: str
    google_oauth_redirect_uri: str
    session_signing_secret: str
    mercado_pago_access_token: str
    mercado_pago_webhook_secret: str
    payment_checkout_return_url: str


@dataclass(frozen=True, slots=True)
class ProcessRuntimeConfig:
    database_url: str
    pool_min_size: int
    pool_max_size: int
    telegram_bot_token: str
    account: AccountRuntimeConfig
    scheduler_interval_seconds: float = 300
    maximum_media_request_size: int = 12 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class EventToolDependencies:
    repository: EventRepository
    interactions: InteractionEventService
    notification_quotas: EventNotificationQuotaService


@dataclass(frozen=True, slots=True)
class RecentMediaToolDependencies:
    store: RecentMediaStore
    downloader: TelegramMediaDownloader


def create_tools_injector(
    settings: LegacyGoogleSheetsSettings | None = None,
    *,
    expense_repository: ExpenseRepository | None = None,
    event_tools: EventToolDependencies | None = None,
    assistant_profile_repository: AssistantProfileRepository | None = None,
    recent_media_tools: RecentMediaToolDependencies | None = None,
) -> ToolsInjector:
    legacy_settings = settings or LegacyGoogleSheetsSettings()
    registrations: list[ToolFamilyRegistration] = [
        create_legacy_google_sheets_registration(
            GoogleSheetsClientFactory(legacy_settings),
        ),
    ]
    if expense_repository is not None:
        registrations.append(
            create_internal_expenses_registration(
                ExpenseService(expense_repository),
            ),
        )
    if event_tools is not None:
        registrations.append(
            create_internal_events_registration(
                EventService(event_tools.repository),
                event_tools.interactions,
                event_tools.notification_quotas,
            ),
        )
    if assistant_profile_repository is not None:
        registrations.append(
            create_internal_profiles_registration(
                AssistantProfileService(assistant_profile_repository),
            ),
        )
    if recent_media_tools is not None:
        registrations.append(
            create_recent_media_registration(
                recent_media_tools.store,
                recent_media_tools.downloader,
            ),
        )
    registry = ToolRegistry(
        registrations=registrations,
    )
    return ToolsInjector(
        registry=registry,
        access_policy=ToolAccessPolicy(
            legacy_settings.LEGACY_GOOGLE_SHEETS_USER_ID,
            profiles_enabled=assistant_profile_repository is not None,
        ),
    )


async def create_process_runtime(
    config: ProcessRuntimeConfig,
    *,
    legacy_google_sheets_settings: LegacyGoogleSheetsSettings | None = None,
) -> ProcessRuntime:
    pool = await create_postgres_pool(
        database_url=config.database_url,
        min_size=config.pool_min_size,
        max_size=config.pool_max_size,
    )
    try:
        await validate_postgres_schema(pool)
    except Exception:
        await pool.close()
        raise

    return _build_process_runtime(
        pool,
        config,
        legacy_google_sheets_settings,
    )


def _build_process_runtime(
    pool: asyncpg.Pool,
    config: ProcessRuntimeConfig,
    legacy_settings: LegacyGoogleSheetsSettings | None,
) -> ProcessRuntime:
    conversations = PostgresConversationRepository(pool)
    event_repository = PostgresEventRepository(pool)
    telegram_updates = PostgresTelegramUpdateRepository(pool)
    notification_quotas = EventNotificationQuotaService(
        PostgresEventNotificationUsageRepository(pool),
    )
    messenger = TelegramMessenger(
        config.telegram_bot_token,
        maximum_media_size=config.maximum_media_request_size,
    )
    recent_media = InMemoryRecentMediaStore()
    users = _create_user_runtime_factory(
        pool,
        conversations,
    )
    account_access = _create_account_access_runtime(
        pool,
        config.account,
        messenger,
        telegram_updates,
        conversations,
    )
    interaction_service = InteractionEventService(
        PostgresInteractionEventRepository(pool),
        subscriptions=account_access.account.subscriptions,
    )
    tools = create_tools_injector(
        legacy_settings,
        expense_repository=PostgresExpenseRepository(pool),
        event_tools=EventToolDependencies(
            repository=event_repository,
            interactions=interaction_service,
            notification_quotas=notification_quotas,
        ),
        assistant_profile_repository=PostgresAssistantProfileRepository(pool),
        recent_media_tools=RecentMediaToolDependencies(
            store=recent_media,
            downloader=messenger,
        ),
    )
    notifications = EventNotificationService(
        preflight=account_access.preflight,
        users=users,
        messenger=messenger,
        quotas=notification_quotas,
        interactions=interaction_service,
        tool_store_builder=lambda resolved_user, timezone: tools.inject_scheduled(
            ToolInjectionContext(
                resolved_user=resolved_user,
                timezone=timezone,
                prompt="Scheduled agent wake-up",
                recent_media=recent_media.list_recent(
                    user_id=resolved_user.user.id,
                ),
            ),
        ),
    )
    return ProcessRuntime(
        pool=pool,
        preflight=account_access.preflight,
        users=users,
        tools=tools,
        messages=MessageCoordinator(
            telegram_updates,
            account_access.preflight.check_rate_limit,
        ),
        telegram=TelegramRuntime(
            messenger=messenger,
            media_downloader=messenger,
            recent_media=recent_media,
            maximum_media_request_size=config.maximum_media_request_size,
            account=account_access.account,
        ),
        scheduler=AgentsScheduler(
            events=EventService(event_repository),
            interactions=interaction_service,
            notifications=notifications,
            interval_seconds=config.scheduler_interval_seconds,
        ),
    )


def _create_account_access_runtime(
    pool: asyncpg.Pool,
    config: AccountRuntimeConfig,
    messenger: OutboundMessenger,
    telegram_updates: PostgresTelegramUpdateRepository,
    conversations: PostgresConversationRepository,
) -> _AccountAccessRuntime:
    accounts = PostgresWebAccountRepository(pool)
    free_subscriptions = FreeSubscriptionService(accounts)
    payment_repository = PostgresPaymentSubscriptionRepository(pool)
    paid_subscriptions = PaidSubscriptionService(
        repository=payment_repository,
        accounts=accounts,
        provider=MercadoPagoClient(
            access_token=config.mercado_pago_access_token,
            webhook_secret=config.mercado_pago_webhook_secret,
        ),
        checkout_return_url=config.payment_checkout_return_url,
    )
    subscriptions = SubscriptionMaintenanceService(
        free=free_subscriptions,
        paid=paid_subscriptions,
    )
    preflight = PreflightService(
        accounts=PostgresAccountRepository(pool),
        conversations=conversations,
        subscriptions=subscriptions,
    )
    sessions = SessionService(
        PostgresBrowserSessionRepository(pool),
        config.session_signing_secret,
    )
    link_repository = PostgresTelegramLinkRepository(pool)
    links = TelegramLinkService(
        link_repository,
        config.telegram_bot_username,
    )
    account = AccountRuntime(
        google_auth=GoogleAuthService(
            provider=GoogleOAuthClient(
                client_id=config.google_oauth_client_id,
                client_secret=config.google_oauth_client_secret,
                redirect_uri=config.google_oauth_redirect_uri,
            ),
            accounts=accounts,
            sessions=sessions,
            signing_secret=config.session_signing_secret,
        ),
        sessions=sessions,
        accounts=WebAccountService(
            accounts=accounts,
            sessions=sessions,
            telegram_links=link_repository,
            subscriptions=subscriptions,
        ),
        telegram_links=links,
        telegram_link_commands=TelegramLinkCommandService(
            links=links,
            updates=telegram_updates,
            messenger=messenger,
            rate_limiter=preflight.check_rate_limit,
        ),
        paid_subscriptions=paid_subscriptions,
        subscriptions=subscriptions,
    )
    return _AccountAccessRuntime(account=account, preflight=preflight)


def _create_user_runtime_factory(
    pool: asyncpg.Pool,
    conversations: PostgresConversationRepository,
) -> UserRuntimeFactory:
    def conversation_store(
        user_id: UUID,
        chat_id: int,
    ) -> ConversationStore:
        return PostgresConversationStore(
            repository=conversations,
            user_id=user_id,
            telegram_chat_id=chat_id,
        )

    return UserRuntimeFactory(
        user_profiles=PostgresUserProfileRepository(pool),
        assistant_profiles=PostgresAssistantProfileRepository(pool),
        conversation_store_builder=conversation_store,
    )


async def close_process_runtime(runtime: ProcessRuntime) -> None:
    await runtime.scheduler.stop()
    await runtime.pool.close()
