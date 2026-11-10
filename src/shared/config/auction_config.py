"""Auction policy constants.

Lives in shared/config alongside stripe_client.commission_bps() because it is the same kind
of thing: a money rule the business tunes, not logic a module should own. bid_dao applies
these; it does not decide them.

The bands are the 9 Nov design's (BACKEND_CHANGES B20). Changing them is one edit here —
nothing else hardcodes a step.
"""
from typing import Optional

# (exclusive upper bound in cents, increment in cents), ascending, last bound None.
#
# Banded on the CURRENT HIGH BID, not the starting price, so the step grows as the auction
# climbs — a $25 step is sensible at $900 and meaningless at $90,000. Flat increments are
# explicitly rejected by the spec, and sellers cannot override these: one rule, whole
# platform, so bidders can learn it once.
BID_INCREMENT_BANDS: tuple[tuple[Optional[int], int], ...] = (
    # The 9 Nov design (HandoffBidding), signed off with it.
    (50_000, 2_500),       # under $500           -> $25
    (100_000, 5_000),      # $500 - $999.99       -> $50
    (500_000, 10_000),     # $1,000 - $4,999.99   -> $100
    (1_000_000, 25_000),   # $5,000 - $9,999.99   -> $250
    (None, 50_000),        # $10,000 and above    -> $500
)


def bid_increment_cents(current_cents: int) -> int:
    """The minimum step a new bid must clear the current high bid by."""
    for upper, increment in BID_INCREMENT_BANDS:
        if upper is None or current_cents < upper:
            return increment
    # Unreachable while the last band's bound is None; kept so a mis-edited table fails
    # loudly on the next bid rather than silently returning no increment at all.
    raise ValueError("BID_INCREMENT_BANDS has no open-ended final band.")



# --- the winner's charge (Phase 7a, the 9 Nov design) -----------------------------------------

def _hours(name: str, default: float) -> float:
    import os

    try:
        return max(0.0, float(os.getenv(name, default)))
    except ValueError:
        return default


def payment_fix_hours() -> float:
    """How long a winner has to fix a card that couldn't be charged (design: 12 hours)."""
    return _hours("AUCTION_PAYMENT_FIX_HOURS", 12)


def second_chance_hours() -> float:
    """How long the next bidder has to accept a second-chance offer."""
    return _hours("AUCTION_SECOND_CHANCE_HOURS", 24)


# Automatic retries on the winner's saved card, measured from the first failure. Anything
# past the fix window is skipped; the window's end is the last chance.
PAYMENT_RETRY_AFTER_HOURS = (1, 4, 8)
