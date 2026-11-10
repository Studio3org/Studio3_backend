"""Paying hosts for their tickets once the event is over (decision T-1), and archiving it.

Runs hourly (events.archive_past):

1. **Archive.** A published event that has ended becomes `archived` and its waitlist closes.
2. **Pay.** `EVENT_PAYOUT_DELAY_HOURS` after it ended (24 by default: a day for "the event
   didn't happen" to reach us before the money leaves), the host's share of every paid
   ticket — the price less Studio 3's 8%, less anything refunded — goes to their Stripe
   account in one Transfer. Card fees they still owe from cancelling an earlier event are
   kept back first (spec: "Take them from the host's next payout").

The Transfer follows payouts_service's shape: claim the payout row and commit, call Stripe
with no lock held, then record it and book the ledger together. A crash between the two is
recovered by finding the transfer by its group before creating another. A failure (Stripe, or
a host who can't be paid yet) is retried on later runs, up to MAX_ATTEMPTS, then left for the
admin console.

Escrow is kept: ticket money sits in the platform's balance until this runs.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from src.shared.config import commission as commission_policy
from src.shared.config.database import SessionLocal
from src.shared.config.stripe_client import get_stripe, platform_currency, stripe_configured
from src.shared.models.event import EVENT_ARCHIVED, EVENT_PUBLISHED, Event
from src.shared.models.refund import REFUND_FAILED, REFUND_PENDING, Refund
from src.shared.models.ticketing import (
    DEBT_OPEN,
    DEBT_SETTLED,
    EVENT_PAYOUT_FAILED,
    EVENT_PAYOUT_NOTHING,
    EVENT_PAYOUT_PENDING,
    EVENT_PAYOUT_RELEASED,
    EVENT_PAYOUT_SENDING,
    ORDER_PAID,
    ORDER_PARTLY_REFUNDED,
    ORDER_REFUNDED,
    EventPayout,
    HostFeeDebt,
    TicketOrder,
)
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

# Hourly, so four days: long enough for Stripe to make the ticket money available.
MAX_ATTEMPTS = 96
RETRYABLE = (EVENT_PAYOUT_PENDING, EVENT_PAYOUT_FAILED, EVENT_PAYOUT_SENDING)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def payout_delay() -> timedelta:
    try:
        hours = float(os.getenv("EVENT_PAYOUT_DELAY_HOURS", "24"))
    except ValueError:
        hours = 24.0
    return timedelta(hours=max(0.0, hours))


def transfer_group(event_id) -> str:
    return f"event_{event_id}"


def owed_cents(db, event_id: uuid.UUID) -> int:
    """The host's share of the event's paid tickets, less what was refunded. Exactly what the
    ledger credited and debited to them for this event."""
    total = 0
    for order in db.execute(
        select(TicketOrder).where(
            TicketOrder.event_id == event_id,
            TicketOrder.status.in_((ORDER_PAID, ORDER_PARTLY_REFUNDED, ORDER_REFUNDED)),
            TicketOrder.total_cents > 0,
        )
    ).scalars():
        fee = commission_policy.commission_cents(order.subtotal_cents, order.fee_bps)
        total += (order.subtotal_cents - fee) - (order.refunded_subtotal_cents - order.refunded_fee_cents)
    return max(0, total)


def _refunds_open(db, event_id: uuid.UUID) -> bool:
    return db.execute(
        select(Refund.id).join(TicketOrder, TicketOrder.id == Refund.ticket_order_id).where(
            TicketOrder.event_id == event_id,
            Refund.status.in_((REFUND_PENDING, REFUND_FAILED)),
        ).limit(1)
    ).first() is not None


def _debt_available(db, host_id: uuid.UUID, exclude_payout: uuid.UUID) -> int:
    """What the host still owes, less what other payouts in flight have already kept back."""
    owed = sum(d.outstanding_cents for d in db.execute(
        select(HostFeeDebt).where(HostFeeDebt.host_id == host_id, HostFeeDebt.status == DEBT_OPEN)
        .with_for_update()
    ).scalars())
    reserved = sum(p.debt_recovered_cents for p in db.execute(
        select(EventPayout).where(EventPayout.host_id == host_id, EventPayout.id != exclude_payout,
                                  EventPayout.status.in_(RETRYABLE))
    ).scalars())
    return max(0, owed - reserved)


def _result(payout: EventPayout) -> dict:
    return {"status": payout.status, "amountCents": payout.amount_cents,
            "owedCents": payout.owed_cents, "debtRecoveredCents": payout.debt_recovered_cents,
            "transferId": payout.stripe_transfer_id, "reason": payout.failure_reason}


# --- one event's payout ---------------------------------------------------------------------

def release_event_payout(event_id: uuid.UUID, *, force: bool = False) -> dict:
    """Pay the host for one finished event. Safe to call again; never pays twice."""
    db = SessionLocal()
    try:
        event = db.get(Event, event_id)
        if event is None:
            raise AppError("Event not found.", 404)
        if event.status != EVENT_ARCHIVED:
            raise AppError("Hosts are paid once the event is over.", 409)
        payout = db.execute(
            select(EventPayout).where(EventPayout.event_id == event_id).with_for_update()
        ).scalar_one_or_none()
        if payout is not None and payout.status in (EVENT_PAYOUT_RELEASED, EVENT_PAYOUT_NOTHING):
            return _result(payout)
        if _refunds_open(db, event_id):
            return {"status": "waiting", "reason": "A refund for this event hasn't finished."}

        owed = owed_cents(db, event_id)
        if payout is None:
            payout = EventPayout(id=uuid.uuid4(), event_id=event_id, host_id=event.host_id,
                                 owed_cents=owed, amount_cents=owed, debt_recovered_cents=0,
                                 status=EVENT_PAYOUT_PENDING,
                                 idempotency_key=f"event_payout:{event_id}")
            db.add(payout)
            db.flush()
        if payout.status == EVENT_PAYOUT_SENDING and payout.stripe_transfer_id is None:
            pass  # a crash after claiming: recovered below, by transfer group
        elif payout.attempts >= MAX_ATTEMPTS and not force:
            return _result(payout)
        payout.owed_cents = owed
        if owed <= 0:
            payout.amount_cents = 0
            payout.status = EVENT_PAYOUT_NOTHING
            payout.failure_reason = None
            db.commit()
            return _result(payout)
        recovered = min(owed, _debt_available(db, event.host_id, payout.id))
        payout.debt_recovered_cents = recovered
        payout.amount_cents = owed - recovered

        host = db.get(User, event.host_id)
        blocker = _blocker(db, host, payout)
        if blocker:
            payout.status = EVENT_PAYOUT_FAILED
            payout.failure_reason = blocker
            payout.attempts += 1
            db.commit()
            return _result(payout)

        payout.status = EVENT_PAYOUT_SENDING
        payout.attempts += 1
        db.commit()
        amount, destination, payout_id = payout.amount_cents, host.stripe_account_id, payout.id
    finally:
        db.close()

    if amount == 0 or not stripe_configured():
        transfer_id = None if amount == 0 else f"dev_transfer_event_{event_id}"
        return _finalize(payout_id, transfer_id)

    try:
        existing = _find_transfer(event_id)
        if existing:
            return _finalize(payout_id, existing["id"])
        transfer = get_stripe().Transfer.create(
            amount=amount, currency=platform_currency(), destination=destination,
            transfer_group=transfer_group(event_id),
            metadata={"event_id": str(event_id), "kind": "event_payout"},
            idempotency_key=f"event_payout:{event_id}:{payout_id}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Event payout transfer failed for event %s", event_id)
        _mark_failed(payout_id, str(exc))
        return {"status": EVENT_PAYOUT_FAILED, "reason": str(exc)}
    return _finalize(payout_id, transfer["id"])


def _blocker(db, host: Optional[User], payout: EventPayout) -> Optional[str]:
    from src.shared.ledger import ledger_service

    if host is None:
        return "Host account not found."
    balance = ledger_service.get_seller_balance(db, host.id)
    if balance < payout.owed_cents:
        return (f"Ledger holds only {balance} cents for this host, less than the "
                f"{payout.owed_cents} owed for this event.")
    if stripe_configured() and payout.amount_cents > 0:
        if not host.stripe_account_id:
            return "Host has not completed Stripe Connect onboarding."
        if not host.stripe_payouts_enabled:
            return "Host's Stripe account cannot receive payouts yet."
    return None


def _find_transfer(event_id) -> Optional[dict]:
    try:
        found = get_stripe().Transfer.list(transfer_group=transfer_group(event_id), limit=1)
        return found["data"][0] if found.get("data") else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not list transfers for event %s: %s", event_id, exc)
        return None


def _finalize(payout_id: uuid.UUID, transfer_id: Optional[str]) -> dict:
    from src.modules.payments import money

    db = SessionLocal()
    try:
        payout = db.execute(
            select(EventPayout).where(EventPayout.id == payout_id).with_for_update()
        ).scalar_one()
        if payout.status == EVENT_PAYOUT_RELEASED:
            return _result(payout)
        payout.status = EVENT_PAYOUT_RELEASED
        payout.stripe_transfer_id = transfer_id
        payout.released_at = _now()
        payout.failure_reason = None
        txn = money.book_event_payout(db, payout, commit=False)
        payout.ledger_transaction_id = txn.id
        _apply_recovered(db, payout)
        db.commit()
        logger.info("Event payout for %s: %s cents sent, %s kept for card fees.",
                    payout.event_id, payout.amount_cents, payout.debt_recovered_cents)
        return _result(payout)
    finally:
        db.close()


def _apply_recovered(db, payout: EventPayout) -> None:
    left = payout.debt_recovered_cents
    for debt in db.execute(
        select(HostFeeDebt).where(HostFeeDebt.host_id == payout.host_id,
                                  HostFeeDebt.status == DEBT_OPEN)
        .order_by(HostFeeDebt.created_at).with_for_update()
    ).scalars():
        if left <= 0:
            break
        take = min(left, debt.outstanding_cents)
        debt.recovered_cents += take
        left -= take
        if debt.outstanding_cents == 0:
            debt.status = DEBT_SETTLED
            debt.settled_at = _now()


def _mark_failed(payout_id: uuid.UUID, reason: str) -> None:
    db = SessionLocal()
    try:
        payout = db.execute(
            select(EventPayout).where(EventPayout.id == payout_id).with_for_update()
        ).scalar_one_or_none()
        if payout is not None and payout.status != EVENT_PAYOUT_RELEASED:
            payout.status = EVENT_PAYOUT_FAILED
            payout.failure_reason = reason[:1000]
            db.commit()
    finally:
        db.close()


# --- the hourly job -------------------------------------------------------------------------------

def archive_and_pay(now: Optional[datetime] = None) -> dict:
    from src.modules.events import event_state, lineup_service, waitlist_service

    now = now or _now()
    archived = paid = 0
    db = SessionLocal()
    try:
        ended = db.execute(
            select(Event.id).where(Event.status == EVENT_PUBLISHED, Event.ends_at <= now)
        ).scalars().all()
        for event_id in ended:
            event = db.execute(select(Event).where(Event.id == event_id).with_for_update()).scalar_one()
            if event.status != EVENT_PUBLISHED:
                db.rollback()
                continue
            event_state.transition_event(db, event, EVENT_ARCHIVED, reason="event_over", commit=False)
            waitlist_service.close_for_event(db, event)
            # B26: every approved piece on the bill gets "Exhibited" on its record.
            lineup_service.record_exhibitions(db, event)
            db.commit()
            archived += 1

        cutoff = now - payout_delay()
        due = db.execute(
            select(Event.id).outerjoin(EventPayout, EventPayout.event_id == Event.id).where(
                Event.status == EVENT_ARCHIVED, Event.ends_at <= cutoff,
                (EventPayout.id.is_(None)) | (EventPayout.status.in_(RETRYABLE)),
            )
        ).scalars().all()
    finally:
        db.close()

    for event_id in due:
        try:
            if release_event_payout(event_id).get("status") == EVENT_PAYOUT_RELEASED:
                paid += 1
        except Exception:  # noqa: BLE001 — one event's trouble mustn't stop the rest
            logger.exception("Event payout for %s failed", event_id)
    return {"archived": archived, "paid": paid}


def retry(event_id: uuid.UUID) -> dict:
    """The admin console's Retry: also past MAX_ATTEMPTS."""
    return release_event_payout(event_id, force=True)
