"""Shipping settings (Phase 8). Everything still waiting on an answer from Convelio or the
client is a setting here, so the answer changes a value, not the code.

* CONVELIO_ENABLED — on by default: every shipment goes through Convelio whenever a key is
  set (CONVELIO_API_KEY). `false` switches back to the flat rates — the fallback for an
  environment without Convelio.
* CONVELIO_API_KEY / CONVELIO_BASE_URL — `sk_test_` keys go to the sandbox unless the URL is
  set.
* SHIPPING_QUOTE_VALID_HOURS — how long a quote can be bought on (pending Convelio's answer).
* ORDER_AUTO_COMPLETE_DAYS — D-9: days after delivery before an unconfirmed order completes
  and the artist is paid.
* SHIPPING_INSURANCE_DEFAULT — D-4: insurance pre-ticked.
"""
import os

SANDBOX_URL = "https://api.sandbox.convelio.com/v2"
PRODUCTION_URL = "https://api.convelio.com/v2"


def _flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _number(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


def api_key() -> str:
    return (os.getenv("CONVELIO_API_KEY") or "").strip()


def enabled() -> bool:
    return _flag("CONVELIO_ENABLED", True) and bool(api_key())


def base_url() -> str:
    explicit = (os.getenv("CONVELIO_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    return SANDBOX_URL if api_key().startswith("sk_test_") else PRODUCTION_URL


def quote_valid_hours() -> float:
    return max(1.0, _number("SHIPPING_QUOTE_VALID_HOURS", 24))


def auto_complete_days() -> float:
    return max(0.0, _number("ORDER_AUTO_COMPLETE_DAYS", 7))


def insurance_default() -> bool:
    return _flag("SHIPPING_INSURANCE_DEFAULT", True)


# Days without a Convelio update before a booked shipment is flagged for ops.
STALE_SHIPMENT_DAYS = 10
