"""Single configured Stripe client.

Centralizes key handling so no module reads STRIPE_SECRET_KEY directly and key values are
never logged. `stripe_configured()` lets callers keep the existing dev-mode behaviour
(auto-succeed checkout) when no keys are present.
"""
import os

import stripe

# Pin the API version so a Stripe-side default upgrade can't silently change response
# shapes underneath the webhook handlers.
STRIPE_API_VERSION = "2024-06-20"

_configured = False


def _secret_key() -> str:
    return (os.getenv("STRIPE_SECRET_KEY") or "").strip()


def webhook_secrets() -> list[str]:
    """All configured webhook signing secrets.

    Stripe issues a separate secret per endpoint, and this app needs two: one for
    account events (payments, refunds, transfers) and one for Connect events on
    connected accounts (account.updated). Both endpoints point at the same URL, so
    STRIPE_WEBHOOK_SECRET accepts a comma-separated list and verification tries each.
    """
    raw = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
    return [part.strip() for part in raw.split(",") if part.strip()]


def stripe_configured() -> bool:
    return bool(_secret_key())


def platform_currency() -> str:
    return (os.getenv("PLATFORM_CURRENCY") or "usd").strip().lower()


def commission_bps() -> int:
    """Platform commission in basis points (1000 = 10%). Global for Phase 1."""
    raw = (os.getenv("PLATFORM_COMMISSION_BPS") or "1000").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 1000
    # A rate outside 0-100% is always a misconfiguration; clamping beats charging an
    # artist a negative or >100% commission.
    return max(0, min(value, 10000))


def get_stripe():
    """Return the configured `stripe` module. Raises if no key is set."""
    global _configured
    key = _secret_key()
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY is not set.")
    if not _configured:
        stripe.api_key = key
        stripe.api_version = STRIPE_API_VERSION
        _configured = True
    return stripe


def connect_return_urls() -> tuple[str, str]:
    """(return_url, refresh_url) for Connect onboarding AccountLinks.

    These are deep links back into the mobile app; Account Links are single-use and expire
    within minutes, so refresh_url must land somewhere that mints a fresh one.
    """
    base = (os.getenv("CONNECT_ONBOARDING_BASE_URL") or "https://studio-3.co/connect").rstrip("/")
    return f"{base}/return", f"{base}/refresh"
