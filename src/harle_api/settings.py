from functools import cache
from typing import Literal

from pydantic import model_validator

from harle_utils import Settings


class ApiSettings(Settings):
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_WEBHOOK_SECRET: str
    POSTGRES_DATABASE_URL: str
    POSTGRES_POOL_MIN_SIZE: int = 1
    POSTGRES_POOL_MAX_SIZE: int = 5
    EVENT_SCHEDULER_INTERVAL_SECONDS: float = 300
    MAX_MEDIA_REQUEST_SIZE: int = 12 * 1024 * 1024
    GOOGLE_OAUTH_CLIENT_ID: str
    GOOGLE_OAUTH_CLIENT_SECRET: str
    GOOGLE_OAUTH_REDIRECT_URI: str
    FRONTEND_REDIRECT_URL: str
    FRONTEND_ORIGINS: list[str]
    SESSION_SIGNING_SECRET: str
    SESSION_COOKIE_NAME: str = "harle_session"
    SESSION_COOKIE_SECURE: bool = True
    SESSION_COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"
    OAUTH_STATE_COOKIE_NAME: str = "harle_oauth_state"
    TELEGRAM_BOT_USERNAME: str
    MERCADO_PAGO_ACCESS_TOKEN: str
    MERCADO_PAGO_WEBHOOK_SECRET: str
    PAYMENT_CHECKOUT_RETURN_URL: str

    @model_validator(mode="after")
    def validate_session_cookie(self) -> "ApiSettings":
        if self.SESSION_COOKIE_SAMESITE == "none" and not self.SESSION_COOKIE_SECURE:
            raise ValueError("SameSite none requires a secure session cookie.")
        return self


class CorsSettings(Settings):
    FRONTEND_ORIGINS: list[str] = []


@cache
def get_settings() -> ApiSettings:
    return ApiSettings()


@cache
def get_cors_settings() -> CorsSettings:
    return CorsSettings()
