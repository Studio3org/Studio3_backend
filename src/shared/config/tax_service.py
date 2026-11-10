"""Sales tax for an order (Phase 8; decision S-2 / Convelio plan D-12).

* `SALES_TAX_PROVIDER=flat` (the default): the flat rate on the artwork, as before
  (config/tax.py). Shipping isn't taxed.
* `SALES_TAX_PROVIDER=stripe`: Stripe Tax works it out from the delivery address, for the
  artwork and the shipping — whether shipping is taxable depends on the state. Stripe Tax
  must be switched on in the dashboard, with the states Studio 3 is registered in. The
  calculation is recorded as a Stripe Tax transaction once the order is paid, which is what
  their filing reports read.

Waiting on the accountant: the default stays flat until they agree.
"""
from __future__ import annotations

import os
from typing import Optional

from src.shared.config.tax import tax_cents as flat_tax_cents
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

# Stripe's code for general tangible goods. Fine art is taxed as that in most states.
ARTWORK_TAX_CODE = "txcd_99999999"


def provider() -> str:
    return (os.getenv("SALES_TAX_PROVIDER") or "flat").strip().lower()


def compute(*, artwork_cents: int, shipping_cents: int, address: Optional[dict],
            reference: str) -> tuple[int, Optional[str]]:
    """(tax cents, Stripe Tax calculation id or None)."""
    if provider() != "stripe":
        return flat_tax_cents(artwork_cents), None
    from src.shared.config.stripe_client import get_stripe, stripe_configured

    if not stripe_configured():
        return flat_tax_cents(artwork_cents), None
    address = address or {}
    try:
        calculation = get_stripe().tax.Calculation.create(
            currency="usd",
            line_items=[{"amount": int(artwork_cents), "reference": reference[:200],
                         "tax_code": ARTWORK_TAX_CODE, "tax_behavior": "exclusive"}],
            shipping_cost={"amount": int(shipping_cents)} if shipping_cents else None,
            customer_details={
                "address": {"line1": address.get("line1"), "line2": address.get("line2"),
                            "city": address.get("city"), "state": address.get("state"),
                            "postal_code": address.get("zip"), "country": address.get("country") or "US"},
                "address_source": "shipping",
            },
        )
    except Exception as exc:  # noqa: BLE001 — a tax we can't work out is a checkout we can't price
        logger.exception("Stripe Tax calculation failed for %s", reference)
        raise AppError("We couldn't work out the sales tax for that address. Check it and try again.",
                       502) from exc
    amount = calculation.get("tax_amount_exclusive") if isinstance(calculation, dict) else \
        getattr(calculation, "tax_amount_exclusive", 0)
    calc_id = calculation.get("id") if isinstance(calculation, dict) else getattr(calculation, "id", None)
    return int(amount or 0), calc_id


def record_paid(order) -> None:
    """After payment: turn the calculation into a Stripe Tax transaction (best effort)."""
    if not getattr(order, "tax_calculation_id", None):
        return
    from src.shared.config.stripe_client import get_stripe, stripe_configured

    if not stripe_configured():
        return
    try:
        get_stripe().tax.Transaction.create_from_calculation(
            calculation=order.tax_calculation_id, reference=str(order.id),
            idempotency_key=f"tax_txn:{order.id}",
        )
    except Exception:  # noqa: BLE001 — reported from the dashboard; never fails a payment
        logger.exception("Stripe Tax transaction failed for order %s", order.id)
