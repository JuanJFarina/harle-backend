from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from harle_agent import __version__
from harle_api.exception_handlers import (
    register_exception_handlers,
    register_request_id_middleware,
)
from harle_api.routes import (
    browser_router,
    health_router,
    payments_router,
    telegram_router,
)
from harle_api.routes.health import get_healthcheck
from harle_api.routes.telegram import post_telegram_webhook
from harle_api.runtime import close_runtime, create_runtime
from harle_api.settings import get_cors_settings, get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime = await create_runtime(get_settings())
    app.state.runtime = runtime
    runtime.scheduler.start()
    try:
        yield
    finally:
        await close_runtime(runtime)


harle_app = FastAPI(
    title="Harle Backend",
    description="Telegram webhook for the Harle assistant.",
    version=__version__,
    lifespan=lifespan,
)
register_exception_handlers(harle_app)

settings = get_cors_settings()
harle_app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
)
register_request_id_middleware(harle_app)
harle_app.include_router(health_router)
harle_app.include_router(telegram_router)
harle_app.include_router(payments_router)
harle_app.include_router(browser_router)

__all__ = [
    "get_healthcheck",
    "harle_app",
    "post_telegram_webhook",
]
