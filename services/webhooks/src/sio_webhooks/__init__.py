"""SIO outbound webhooks (PRD M22)."""

from .delivery import (
    BASE_DELAY_S,
    MAX_ATTEMPTS,
    MAX_DELAY_S,
    RETRYABLE_STATUS,
    Attempt,
    Delivery,
    backoff_delay,
    matches,
    should_retry,
)
from .service import FORWARDED, WebhooksService
from .signing import DEFAULT_TOLERANCE_S, SIGNATURE_HEADER, sign, verify

__all__ = [
    "BASE_DELAY_S",
    "DEFAULT_TOLERANCE_S",
    "FORWARDED",
    "MAX_ATTEMPTS",
    "MAX_DELAY_S",
    "RETRYABLE_STATUS",
    "SIGNATURE_HEADER",
    "Attempt",
    "Delivery",
    "WebhooksService",
    "backoff_delay",
    "matches",
    "should_retry",
    "sign",
    "verify",
]
