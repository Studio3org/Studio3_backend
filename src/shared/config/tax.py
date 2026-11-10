"""Sales tax, in whole cents.

One flat rate for now (8.25%, the figure the app has always shown), held in config rather than
code so it can change without a release. Real tax by address is a pending decision (Stripe
Tax, plan S-2); when it lands, this is the one place that changes.

Integer arithmetic, rounding half up — the float version (`round(cents * 0.0825)`) rounded
half to even and could differ by a cent from what a receipt shows (BE-11).
"""
import os

DEFAULT_TAX_RATE_BPS = 825


def tax_rate_bps() -> int:
    return int(os.getenv("SALES_TAX_RATE_BPS", DEFAULT_TAX_RATE_BPS))


def tax_cents(amount_cents: int) -> int:
    if amount_cents <= 0:
        return 0
    return (amount_cents * tax_rate_bps() + 5000) // 10000
