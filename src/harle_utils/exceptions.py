from asyncpg import PostgresError
from google.genai.errors import APIError, ClientError, ServerError
from pydantic import ValidationError


class AccessDeniedError(Exception):
    pass


class UnknownIdentityError(AccessDeniedError):
    pass


class InactiveSubscriptionError(AccessDeniedError):
    pass


class MissingProfileError(AccessDeniedError):
    pass


class ToolAccessDeniedError(AccessDeniedError):
    pass


class ToolUnavailableError(ValueError):
    pass


class InvalidDatabaseSchemaError(RuntimeError):
    pass


class MessageDeliveryError(RuntimeError):
    pass


class MediaDownloadError(RuntimeError):
    pass


class AuthenticationRequiredError(AccessDeniedError):
    pass


class InvalidCsrfError(AccessDeniedError):
    pass


class InvalidOAuthError(AccessDeniedError):
    pass


class OAuthProviderError(RuntimeError):
    pass


class TelegramAlreadyLinkedError(AccessDeniedError):
    pass


class SubscriptionConflictError(AccessDeniedError):
    pass


class InvalidPaymentWebhookError(AccessDeniedError):
    pass


class PaymentProviderError(RuntimeError):
    pass


class PaymentProviderRejectedError(PaymentProviderError):
    pass


ASSISTANT_FAILURES = (
    APIError,
    ClientError,
    ServerError,
    RuntimeError,
    ToolAccessDeniedError,
    ValueError,
    ValidationError,
)

PROCESSING_FAILURES = (*ASSISTANT_FAILURES, OSError, PostgresError)
