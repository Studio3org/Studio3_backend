"""Platform commission policy.

The client's rates: 20% on original art, 15% for a named set of artists, 8% on event
tickets — and those percentages are inclusive of Stripe's processing fee, which the platform
absorbs out of its own cut rather than adding to what the buyer pays.

Two things this module exists to keep straight:

* **Commission is inclusive.** It comes out of the price the artist set, never on top of it.
  $100 of art at 20% means the buyer pays $100 and the artist receives $80.
* **The rate that applies is the rate at the time of sale.** Callers resolve it once and
  store it on the row; nothing recomputes from config afterwards, or changing the rate
  tomorrow would silently restate what every past artist is owed.
"""
import os

from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

# What is being sold. The rate differs by kind, not just by seller.
SALE_ART = "art"
SALE_TICKET = "ticket"
SALE_KINDS = (SALE_ART, SALE_TICKET)

_DEFAULTS = {
    SALE_ART: 2000,     # 20%
    SALE_TICKET: 800,   # 8%
}


# Stripe's published rate, used only to warn about sales too small to cover it. Not used in
# any charge calculation — the real fee comes from the charge's balance_transaction.
STRIPE_PERCENT_BPS = 290
STRIPE_FIXED_CENTS = 30


def _clamp(key: str, raw: str | None, default: int) -> int:
    raw = (raw or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s is not an integer (%r); using %d bps.", key, raw, default)
        return default
    if value < 0 or value > 10000:
        # A rate outside 0-100% is always a misconfiguration, and clamping beats charging an
        # artist a negative or greater-than-total commission.
        logger.warning("%s of %d bps is out of range; clamping.", key, value)
        return max(0, min(value, 10000))
    return value


def default_bps(sale_kind: str = SALE_ART) -> int:
    """The standard rate for this kind of sale, before any per-seller override.

    The os.getenv calls below use literal names on purpose. tests/unit/test_config_contract
    finds environment reads by walking the AST for literals, so a key held in a dict and
    passed to a helper is invisible to it — which is exactly how
    PLATFORM_TICKET_COMMISSION_BPS came to be read without ever being declared.
    """
    if sale_kind not in SALE_KINDS:
        raise ValueError(f"Unknown sale kind {sale_kind!r}; expected one of {SALE_KINDS}.")
    if sale_kind == SALE_ART:
        return _clamp(
            "PLATFORM_COMMISSION_BPS",
            os.getenv("PLATFORM_COMMISSION_BPS"),
            _DEFAULTS[SALE_ART],
        )
    return _clamp(
        "PLATFORM_TICKET_COMMISSION_BPS",
        os.getenv("PLATFORM_TICKET_COMMISSION_BPS"),
        _DEFAULTS[SALE_TICKET],
    )


def resolve_bps(seller, sale_kind: str = SALE_ART) -> int:
    """The rate that applies to this seller for this kind of sale.

    A per-seller override is how the client's reduced tier for a named set of artists is
    expressed: it is a property of the relationship, not of the listing, so it lives on the
    user rather than being re-entered per piece.
    """
    override = getattr(seller, "commission_bps_override", None) if seller is not None else None
    if override is not None and sale_kind == SALE_ART:
        return max(0, min(int(override), 10000))
    return default_bps(sale_kind)


def commission_cents(amount_cents: int, bps: int) -> int:
    """Commission on `amount_cents` at an explicit rate.

    The rate is a required argument on purpose. Reading it from config here is what made
    the old implementation restate historical payouts whenever the number changed.
    """
    return round(amount_cents * bps / 10000)


def net_cents(amount_cents: int, bps: int) -> int:
    """What the seller receives — the inclusive half of the same calculation."""
    return amount_cents - commission_cents(amount_cents, bps)


def covers_stripe_fee(amount_cents: int, bps: int) -> bool:
    """Whether the commission on this sale is larger than Stripe will charge for it.

    False means the platform loses money on the transaction. At the client's 8% ticket rate
    that is every ticket under about $5.88, which is worth surfacing rather than discovering
    in a month-end reconciliation.
    """
    fee = round(amount_cents * STRIPE_PERCENT_BPS / 10000) + STRIPE_FIXED_CENTS
    return commission_cents(amount_cents, bps) >= fee


def break_even_cents(bps: int) -> int:
    """The smallest sale whose commission covers Stripe's fee, for the given rate."""
    margin = bps - STRIPE_PERCENT_BPS
    if margin <= 0:
        return 0  # no sale is large enough; the rate never beats Stripe's percentage
    return -(-STRIPE_FIXED_CENTS * 10000 // margin)  # ceiling division
