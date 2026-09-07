"""Artist payout release — internal only, never a client-facing route (FR-6.1).

The hard problem here is that a Stripe API call cannot be made atomically with a DB commit.
The sequence is therefore:

    1. Lock the payout row, verify state, mark ready_to_release, COMMIT.
       (claims the work — the lock stops a second worker claiming the same payout)
    2. Call Stripe.
    3. In one transaction: mark released, store the transfer id, book the ledger entry.

If the process dies between 2 and 3, the payout is stuck in ready_to_release while a real
transfer may exist. `_find_existing_transfer` recovers that case by looking up the
transfer_group before creating anything, which is what closes the gap left by Stripe's
idempotency keys expiring after 24 hours.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.shared.config.database import SessionLocal
from src.shared.config.stripe_client import get_stripe, platform_currency, stripe_configured
from src.shared.ledger import ledger_service
from src.shared.models.order import Order
from src.shared.models.payout import (
    PAYOUT_PENDING,
    PAYOUT_READY_TO_RELEASE,
    PAYOUT_RELEASED,
    PAYOUT_TRANSFER_FAILED,
    Payout,
)
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.payments import money

logger = get_logger(__name__)

RELEASABLE_STATUSES = (PAYOUT_PENDING, PAYOUT_READY_TO_RELEASE, PAYOUT_TRANSFER_FAILED)


def transfer_group(order_id) -> str:
    return f"order_{order_id}"


def _find_existing_transfer(order_id) -> Optional[dict]:
    """Look for a transfer already created for this order.

    Crash recovery: if we died after calling Stripe but before recording the transfer id,
    this finds it so a retry adopts the existing transfer instead of paying twice.
    """
    stripe = get_stripe()
    try:
        found = stripe.Transfer.list(transfer_group=transfer_group(order_id), limit=1)
        return found["data"][0] if found.get("data") else None
    except Exception as e:
        logger.warning("Could not list transfers for order %s: %s", order_id, e)
        return None


def _result(status: str, reason: str = None, transfer_id: str = None, amount_cents: int = 0) -> dict:
    """Plain-dict result. These functions close their DB session before returning, so
    handing back an ORM instance would raise DetachedInstanceError on attribute access."""
    return {
        "status": status,
        "reason": reason,
        "transferId": transfer_id,
        "amountCents": amount_cents,
    }


def release_payout_for_order(order_id: uuid.UUID) -> dict:
    """Release the artist's payout for a completed order.

    Safe to call more than once: it no-ops if the payout is already released. Never raises
    on a Stripe failure — the payout is marked transfer_failed and surfaced in the admin
    queue for retry, because by this point the collector has already confirmed delivery and
    that fact must not be rolled back.
    """
    db = SessionLocal()
    try:
        payout = db.execute(
            select(Payout).where(Payout.order_id == order_id).with_for_update()
        ).scalar_one_or_none()
        if not payout:
            logger.error("No payout row for order %s.", order_id)
            return _result("missing", "No payout row for this order.")
        if payout.status == PAYOUT_RELEASED:
            logger.info("Payout for order %s already released.", order_id)
            return _result(PAYOUT_RELEASED, transfer_id=payout.stripe_transfer_id)

        order = db.get(Order, order_id)
        if not order or order.status != "completed":
            logger.warning(
                "Refusing payout for order %s in status %s.",
                order_id, order.status if order else "missing",
            )
            return _result(payout.status, "Order is not completed.")
        if payout.status not in RELEASABLE_STATUSES:
            return _result(payout.status, "Payout is not in a releasable state.")

        seller = db.get(User, payout.seller_id)
        amount = ledger_service.get_seller_balance(db, payout.seller_id)

        blocker = _blocking_reason(seller, amount)
        if blocker:
            payout.status = PAYOUT_TRANSFER_FAILED
            payout.failure_reason = blocker
            db.commit()
            logger.error("Payout blocked for order %s: %s", order_id, blocker)
            return _result(PAYOUT_TRANSFER_FAILED, blocker)

        # Step 1: claim the work and commit before touching Stripe.
        payout.status = PAYOUT_READY_TO_RELEASE
        db.commit()

        destination = seller.stripe_account_id
        idem = payout.idempotency_key
    finally:
        db.close()

    if not stripe_configured():
        # Dev mode: no Stripe keys. Book the ledger so the escrow flow is still testable.
        return _finalize(order_id, transfer_id=f"dev_transfer_{order_id}", amount_cents=amount)

    # Step 2: Stripe call, outside any open transaction.
    try:
        existing = _find_existing_transfer(order_id)
        if existing:
            logger.warning(
                "Adopting existing transfer %s for order %s (crash recovery).",
                existing["id"], order_id,
            )
            return _finalize(order_id, existing["id"], int(existing["amount"]))

        stripe = get_stripe()
        kwargs = {
            "amount": amount,
            "currency": platform_currency(),
            "destination": destination,
            "transfer_group": transfer_group(order_id),
            "metadata": {"order_id": str(order_id)},
            "idempotency_key": idem,
        }
        # source_transaction ties the transfer to the original charge so Stripe releases
        # funds as that charge settles, instead of failing with balance_insufficient when
        # the platform balance hasn't cleared yet.
        db2 = SessionLocal()
        try:
            order = db2.get(Order, order_id)
            if order and order.stripe_charge_id:
                kwargs["source_transaction"] = order.stripe_charge_id
        finally:
            db2.close()

        transfer = stripe.Transfer.create(**kwargs)
    except Exception as e:
        _mark_failed(order_id, str(e))
        logger.exception("Transfer failed for order %s: %s", order_id, e)
        return _result(PAYOUT_TRANSFER_FAILED, str(e))

    # Step 3: record success + ledger, atomically.
    return _finalize(order_id, transfer["id"], int(transfer["amount"]))


def _blocking_reason(seller: User, amount_cents: int) -> Optional[str]:
    if not seller:
        return "Seller account not found."
    if amount_cents <= 0:
        return f"Nothing owed to this artist (balance {amount_cents} cents)."
    # Connect checks only apply when there is a real Stripe to pay through; in dev mode no
    # artist has an account, and blocking there would make the escrow flow untestable.
    if stripe_configured():
        if not seller.stripe_account_id:
            return "Artist has not completed Stripe Connect onboarding."
        if not seller.stripe_payouts_enabled:
            return "Artist's Stripe account cannot receive payouts yet."
    return None


def _finalize(order_id: uuid.UUID, transfer_id: str, amount_cents: int) -> dict:
    db = SessionLocal()
    try:
        payout = db.execute(
            select(Payout).where(Payout.order_id == order_id).with_for_update()
        ).scalar_one_or_none()
        if not payout:
            return _result("missing", "No payout row for this order.")
        if payout.status == PAYOUT_RELEASED:
            return _result(PAYOUT_RELEASED, transfer_id=payout.stripe_transfer_id)
        order = db.get(Order, order_id)

        payout.status = PAYOUT_RELEASED
        payout.stripe_transfer_id = transfer_id
        payout.released_at = datetime.now(timezone.utc)
        payout.failure_reason = None

        txn = money.book_payout_released(db, order, amount_cents, commit=False)
        payout.ledger_transaction_id = txn.id
        db.commit()
        logger.info("Payout released for order %s: %s (%d cents).", order_id, transfer_id, amount_cents)
        return _result(PAYOUT_RELEASED, transfer_id=transfer_id, amount_cents=amount_cents)
    finally:
        db.close()


def _mark_failed(order_id: uuid.UUID, reason: str) -> None:
    db = SessionLocal()
    try:
        payout = db.execute(
            select(Payout).where(Payout.order_id == order_id).with_for_update()
        ).scalar_one_or_none()
        if payout and payout.status != PAYOUT_RELEASED:
            payout.status = PAYOUT_TRANSFER_FAILED
            payout.failure_reason = reason[:1000]
            db.commit()
    finally:
        db.close()


def retry_payout(order_id: uuid.UUID) -> dict:
    """Admin-triggered retry for a failed payout. Goes through the same recovery path, so a
    retry can never create a second transfer for an order that already has one."""
    db = SessionLocal()
    try:
        payout = db.execute(
            select(Payout).where(Payout.order_id == order_id)
        ).scalar_one_or_none()
        if not payout:
            raise AppError("No payout exists for this order.", 404)
        if payout.status == PAYOUT_RELEASED:
            raise AppError("This payout has already been released.", 409)
        order = db.get(Order, order_id)
        if not order or order.status != "completed":
            raise AppError("Payout can only be released on a completed order.", 409)
    finally:
        db.close()
    return release_payout_for_order(order_id)
