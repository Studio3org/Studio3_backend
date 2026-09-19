"""Auction policy constants.

Lives in shared/config alongside stripe_client.commission_bps() because it is the same kind
of thing: a money rule the business tunes, not logic a module should own. bid_dao applies
these; it does not decide them.

The bands below are a proposal pending client sign-off (see the Bidding & Events spec,
"Proposed bid increments"). Changing them is one edit here — nothing else hardcodes a step.
"""
from typing import Optional

# (exclusive upper bound in cents, increment in cents), ascending, last bound None.
#
# Banded on the CURRENT HIGH BID, not the starting price, so the step grows as the auction
# climbs — a $25 step is sensible at $900 and meaningless at $90,000. Flat increments are
# explicitly rejected by the spec, and sellers cannot override these: one rule, whole
# platform, so bidders can learn it once.
BID_INCREMENT_BANDS: tuple[tuple[Optional[int], int], ...] = (
    (10_000, 500),        # under $100          -> $5
    (50_000, 1_000),      # $100 - $499.99      -> $10
    (200_000, 2_500),     # $500 - $1,999.99    -> $25
    (1_000_000, 10_000),  # $2,000 - $9,999.99  -> $100
    (None, 50_000),       # $10,000 and above   -> $500
)


def bid_increment_cents(current_cents: int) -> int:
    """The minimum step a new bid must clear the current high bid by."""
    for upper, increment in BID_INCREMENT_BANDS:
        if upper is None or current_cents < upper:
            return increment
    # Unreachable while the last band's bound is None; kept so a mis-edited table fails
    # loudly on the next bid rather than silently returning no increment at all.
    raise ValueError("BID_INCREMENT_BANDS has no open-ended final band.")
