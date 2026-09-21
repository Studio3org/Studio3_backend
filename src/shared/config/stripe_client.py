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

# Endpoints in the /v2 namespace reject the v1 pin above — they require a version new enough
# to know about Accounts v2. Rather than move the whole integration forward (which would
# change every webhook payload shape the handlers are written against), v2 calls go through
# their own client on their own version. The two are deliberately independent.
STRIPE_V2_API_VERSION = "2026-08-26.dahlia"

_configured = False
_v2_client = None


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


def get_stripe_v2():
    """Return a StripeClient bound to the /v2 API version. Raises if no key is set.

    Used only for Accounts v2 (`/v2/core/accounts`, `/v2/core/account_links`). Everything
    else stays on `get_stripe()`: v2 account ids are accepted by the v1 endpoints and come
    back in the v1 shape, so reads, transfers and login links did not have to move.
    """
    global _v2_client
    key = _secret_key()
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY is not set.")
    if _v2_client is None:
        _v2_client = stripe.StripeClient(key, stripe_version=STRIPE_V2_API_VERSION)
    return _v2_client


def payouts_ready(account) -> bool:
    """Whether Stripe will actually let a connected account receive its money.

    Reads the v1 account shape, which is what Stripe returns for a v2 account on the v1
    endpoints and in the `account.updated` payload.

    Deliberately not `charges_enabled`: artists are created with the recipient configuration
    only and never request a charge capability, so `charges_enabled` stays false forever.
    Gating on it — as this did — would have held every payout and blocked every for-sale
    listing permanently, the first time an artist actually finished onboarding.
    """
    capabilities = account.get("capabilities") or {}
    return bool(account.get("payouts_enabled")) and capabilities.get("transfers") == "active"


def connect_return_urls() -> tuple[str, str]:
    """(return_url, refresh_url) for Connect onboarding AccountLinks.

    These are deep links back into the mobile app; Account Links are single-use and expire
    within minutes, so refresh_url must land somewhere that mints a fresh one.
    """
    base = (os.getenv("CONNECT_ONBOARDING_BASE_URL") or "https://studio-3.co/connect").rstrip("/")
    return f"{base}/return", f"{base}/refresh"
