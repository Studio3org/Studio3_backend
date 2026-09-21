"""Checking the books against reality.

The ledger is internally consistent by construction — every transaction is balanced before it
is written, and `assert_ledger_balanced` proves debits equal credits. What that cannot catch
is the ledger being consistently *wrong*: a Stripe charge that succeeded while our write
failed, a fee we estimated rather than read, a refund issued in the dashboard by a human that
the webhook never told us about.

Those are exactly the errors that do not announce themselves. Nothing breaks, no request
fails; the books simply drift from the money, and the first anyone knows is a payout that
cannot be made because the balance was never really there.

So this compares what the ledger says the platform is holding against what Stripe says, and
reports the difference. It **never corrects anything**. An automatic correction would paper
over the bug that caused the drift and destroy the evidence of it; the useful output is a
number and a date range for a person to go and look at.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.shared.config.database import SessionLocal
from src.shared.config.stripe_client import get_stripe, platform_currency, stripe_configured
from src.shared.ledger import ledger_service
from src.shared.models.audit import ACTOR_SYSTEM
from src.shared.models.ledger import (
    ACCOUNT_AUCTION_ESCROW,
    ACCOUNT_PLATFORM_CLEARING,
    ACCOUNT_SELLER_PAYABLE,
    ACCOUNT_TAX_PAYABLE,
    LedgerAccount,
)
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

# How far apart the two may be before it is worth waking somebody. Not zero: Stripe's balance
# moves continuously and a charge landing mid-read is an ordinary race, not a defect. A dollar
# is small enough that a real accounting error clears it easily.
TOLERANCE_CENTS = 100


def reconcile(db=None) -> dict:
    """Compare the ledger's view of the platform balance against Stripe's.

    Returns the comparison rather than raising: this runs on a scheduler with no caller to
    report to, and a drift is a finding to record, not an error to crash on.
    """
    owns_session = db is None
    db = db or SessionLocal()
    try:
        ledger_cents = _ledger_clearing_balance(db)
        stripe_cents = _stripe_balance_cents()

        result = {
            "checkedAt": datetime.now(timezone.utc).isoformat(),
            "ledgerClearingCents": ledger_cents,
            "stripeBalanceCents": stripe_cents,
            "liabilities": _liabilities(db),
        }

        if stripe_cents is None:
            # Not a discrepancy. Stripe being unconfigured is the normal state in dev and on
            # staging, and reporting it as drift would train people to ignore this.
            result["status"] = "skipped"
            result["reason"] = "Stripe is not configured; nothing to compare against."
            return result

        difference = ledger_cents - stripe_cents
        result["differenceCents"] = difference
        result["status"] = "ok" if abs(difference) <= TOLERANCE_CENTS else "drift"

        if result["status"] == "drift":
            # Loud, because the whole point is that this failure is otherwise silent.
            logger.error(
                "Ledger drift: clearing says %d cents, Stripe says %d cents (difference %d)",
                ledger_cents, stripe_cents, difference,
            )
            _record_drift(db, result)
        else:
            logger.info(
                "Ledger reconciled: %d cents, within tolerance of Stripe's %d.",
                ledger_cents, stripe_cents,
            )
        return result
    finally:
        if owns_session:
            db.close()


def _ledger_clearing_balance(db) -> int:
    """What the books say is sitting in the Stripe balance.

    platform_clearing is the asset account money lands in and leaves from, so it is the one
    that should track Stripe's own figure.
    """
    account = ledger_service.get_platform_account(
        db, ACCOUNT_PLATFORM_CLEARING, platform_currency().upper()
    )
    return ledger_service.get_balance(db, account)


def _stripe_balance_cents() -> Optional[int]:
    """Stripe's own figure, available plus pending.

    Both, deliberately: a charge that has settled and one that has not are both money the
    platform has taken, and the ledger does not distinguish them. Comparing against available
    alone would report drift every time a charge was in flight.
    """
    if not stripe_configured():
        return None
    try:
        balance = get_stripe().Balance.retrieve()
    except Exception:
        logger.exception("Could not read the Stripe balance")
        return None

    currency = platform_currency().lower()
    total = 0
    for bucket in ("available", "pending"):
        for entry in balance.get(bucket) or []:
            if (entry.get("currency") or "").lower() == currency:
                total += int(entry.get("amount") or 0)
    return total


def _liabilities(db) -> dict:
    """What the platform owes out of that balance.

    Not part of the comparison — these are claims on the money, not a second opinion about
    how much there is. Reported alongside because a clearing balance that matches Stripe is
    still a problem if it is smaller than what is owed out of it.
    """
    currency = platform_currency().upper()
    owed = {}
    for account_type in (ACCOUNT_SELLER_PAYABLE, ACCOUNT_TAX_PAYABLE, ACCOUNT_AUCTION_ESCROW):
        if account_type == ACCOUNT_SELLER_PAYABLE:
            # Per-seller accounts, so this one is a sum rather than a singleton.
            owed[account_type] = _seller_payable_total(db)
            continue
        account = ledger_service.get_platform_account(db, account_type, currency)
        owed[account_type] = ledger_service.get_balance(db, account)
    return owed


def _seller_payable_total(db) -> int:
    from sqlalchemy import select

    total = 0
    for account in db.execute(
        select(LedgerAccount).where(LedgerAccount.type == ACCOUNT_SELLER_PAYABLE)
    ).scalars():
        total += ledger_service.get_balance(db, account)
    return total


def _record_drift(db, result: dict) -> None:
    """Leave a durable trace of the finding.

    A log line is not enough for exactly the reason the audit table exists: on Render it is
    gone within the day, and drift is something somebody may only investigate a week later.
    """
    from src.modules.admin import audit_service

    audit_service.system(
        db,
        "ledger_drift_detected",
        subject_type="ledger",
        detail={
            "ledgerClearingCents": result["ledgerClearingCents"],
            "stripeBalanceCents": result["stripeBalanceCents"],
            "differenceCents": result["differenceCents"],
        },
        note=(
            "Ledger and Stripe disagree about the platform balance. Nothing has been "
            "corrected — investigate before adjusting anything."
        ),
    )


def recent_drift(db, days: int = 30) -> list:
    """Drift findings from the last month, for the admin page."""
    from src.modules.admin import audit_service

    since = datetime.now(timezone.utc) - timedelta(days=days)
    return [
        event
        for event in audit_service.recent(db, action="ledger_drift_detected", limit=50)
        if event.created_at >= since
    ]


__all__ = ["reconcile", "recent_drift", "TOLERANCE_CENTS", "ACTOR_SYSTEM"]
