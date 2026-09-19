"""Error tracking, initialised only when SENTRY_DSN is set.

Gating on the DSN keeps development and CI completely unaffected — no network calls, no
fixtures, no test doubles. Errors only: performance tracing on a single free-tier worker is
noise, and metrics/APM are disproportionate for this deployment.
"""
import os
from typing import Any, Optional

from src.shared.utils.logger import get_logger
from src.shared.utils.request_id import current_request_id

logger = get_logger(__name__)

# Keys whose values must never leave the building. StripeWebhookEvent.payload carries buyer
# name, address and email, and the order snapshot is a full postal address.
_SCRUBBED_KEYS = frozenset({
    "payload",
    "shipping_address_snapshot",
    "shippingAddress",
    "password",
    "token",
    "client_secret",
    "clientSecret",
    "authorization",
})


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return value
    if isinstance(value, dict):
        return {
            k: ("[scrubbed]" if k.lower() in _SCRUBBED_KEYS else _scrub(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v, depth + 1) for v in value]
    return value


def _before_send(event: dict, hint: Optional[dict] = None) -> dict:
    """Drop PII and tag the event with our correlation id, so a Sentry issue and a log line
    can be joined."""
    event["extra"] = _scrub(event.get("extra") or {})
    if request_context := event.get("request"):
        event["request"] = _scrub(request_context)
    tags = event.setdefault("tags", {})
    tags["request_id"] = current_request_id()
    return event


def init_sentry(environment: str) -> bool:
    """Returns whether Sentry was initialised. Never raises — a monitoring dependency must
    not be what stops the service from starting."""
    dsn = (os.getenv("SENTRY_DSN") or "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration

        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            integrations=[FlaskIntegration()],
            send_default_pii=False,
            traces_sample_rate=0.0,
            release=os.getenv("RENDER_GIT_COMMIT"),
            before_send=_before_send,
        )
        logger.info("Sentry initialised for %s.", environment)
        return True
    except Exception:
        logger.exception("Sentry init failed; continuing without error tracking.")
        return False
