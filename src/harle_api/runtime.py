from harle_api.settings import ApiSettings
from harle_services.bootstrap import (
    AccountRuntimeConfig,
    ProcessRuntime,
    ProcessRuntimeConfig,
    close_process_runtime,
    create_process_runtime,
)

ApiRuntime = ProcessRuntime


async def create_runtime(settings: ApiSettings) -> ApiRuntime:
    return await create_process_runtime(
        ProcessRuntimeConfig(
            database_url=settings.POSTGRES_DATABASE_URL,
            pool_min_size=settings.POSTGRES_POOL_MIN_SIZE,
            pool_max_size=settings.POSTGRES_POOL_MAX_SIZE,
            telegram_bot_token=settings.TELEGRAM_BOT_TOKEN,
            account=AccountRuntimeConfig(
                telegram_bot_username=settings.TELEGRAM_BOT_USERNAME,
                google_oauth_client_id=settings.GOOGLE_OAUTH_CLIENT_ID,
                google_oauth_client_secret=settings.GOOGLE_OAUTH_CLIENT_SECRET,
                google_oauth_redirect_uri=settings.GOOGLE_OAUTH_REDIRECT_URI,
                session_signing_secret=settings.SESSION_SIGNING_SECRET,
                mercado_pago_access_token=settings.MERCADO_PAGO_ACCESS_TOKEN,
                mercado_pago_webhook_secret=settings.MERCADO_PAGO_WEBHOOK_SECRET,
                mercado_pago_testing=settings.MERCADO_PAGO_TESTING,
                mercado_pago_test_payer_email=(settings.MERCADO_PAGO_TEST_PAYER_EMAIL),
                payment_checkout_return_url=settings.PAYMENT_CHECKOUT_RETURN_URL,
            ),
            scheduler_interval_seconds=settings.EVENT_SCHEDULER_INTERVAL_SECONDS,
            maximum_media_request_size=settings.MAX_MEDIA_REQUEST_SIZE,
        ),
    )


async def close_runtime(runtime: ApiRuntime) -> None:
    await close_process_runtime(runtime)
