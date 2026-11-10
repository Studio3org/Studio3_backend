"""The host moving or calling off an event (ticketing spec, "Refunds, rescheduling and
cancelling"; BACKEND_CHANGES B28).

**Reschedule.** A new start and end, and an optional message. Tickets keep working, every
ticket holder is told once (push and email), and anyone can get a full refund until the new
start (ticket_refunds.refundable_until). The event's auctions move with it — unless bidding
has started, when the reschedule is refused: moving a clock people are bidding against is
not ours to do quietly. Wallet passes update.

**Cancel.** Before confirming, the host sees the tickets to refund, the total and the card
fees they'll pay (`cancel_preview`). On confirm, in one transaction: the event is cancelled,
its listings come down (lineup_service, as before), every ticket stops working, unpaid
checkouts are called off, the waitlist is cleared, and the card fees are booked as owed by
the host. Then, in the background, every paid order is refunded in full and guests and the
waitlist are told, with the host's message.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.shared.config.stripe_client import get_stripe, stripe_configured
from src.shared.models.event import (
    EVENT_PUBLISHED,
    PIECE_BID,
    RSVP_GOING,
    Event,
    EventPiece,
    EventRsvp,
)
from src.shared.models.ticketing import (
    DEBT_OPEN,
    LIVE_TICKET_STATUSES,
    ORDER_CANCELLED,
    ORDER_PAID,
    ORDER_PARTLY_REFUNDED,
    ORDER_PENDING,
    HostFeeDebt,
    Ticket,
    TicketOrder,
)
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

MESSAGE_MAX = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_message(raw) -> Optional[str]:
    text = (raw or "").strip() if isinstance(raw, str) else ""
    return text[:MESSAGE_MAX] or None


# --- what a cancellation costs ----------------------------------------------------------------

def _paid_orders(db: Session, event_id: uuid.UUID, *, lock: bool = False) -> list[TicketOrder]:
    stmt = select(TicketOrder).where(
        TicketOrder.event_id == event_id,
        TicketOrder.status.in_((ORDER_PAID, ORDER_PARTLY_REFUNDED)),
        TicketOrder.total_cents > 0,
    )
    if lock:
        stmt = stmt.with_for_update()
    return list(db.execute(stmt).scalars())


def stripe_fee_for(db: Session, order: TicketOrder) -> int:
    """The fee Stripe kept on this payment. Stored since Phase 4; for older orders, read back
    from the ledger posting made when it was paid."""
    if order.stripe_fee_cents is not None:
        return order.stripe_fee_cents
    from src.shared.models.ledger import (
        ACCOUNT_STRIPE_FEES,
        DEBIT,
        LedgerAccount,
        LedgerEntry,
        LedgerTransaction,
    )

    fee = db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0))
        .join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.transaction_id)
        .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
        .where(LedgerTransaction.idempotency_key == f"ticket_order_paid:{order.id}",
               LedgerAccount.type == ACCOUNT_STRIPE_FEES, LedgerEntry.direction == DEBIT)
    ).scalar_one()
    return int(fee)


def cancel_preview(db: Session, event: Event) -> dict:
    """Spec: "show the host: tickets to refund, total refunded, and the card processing fees
    they pay"."""
    orders = _paid_orders(db, event.id)
    tickets = db.execute(
        select(func.count(Ticket.id)).where(Ticket.event_id == event.id,
                                            Ticket.status.in_(LIVE_TICKET_STATUSES))
    ).scalar_one()
    from src.shared.models.ticketing import WAITLIST_IN_LINE, WaitlistEntry

    waiting = db.execute(
        select(func.count(WaitlistEntry.id)).where(WaitlistEntry.event_id == event.id,
                                                   WaitlistEntry.status.in_(WAITLIST_IN_LINE))
    ).scalar_one()
    return {
        "ticketsToVoid": int(tickets),
        "ordersToRefund": len(orders),
        "refundTotalCents": sum(o.total_cents - o.refunded_cents for o in orders),
        "cardFeesCents": sum(stripe_fee_for(db, o) for o in orders),
        "waitlistCount": int(waiting),
        "guestsToNotify": len(_ticket_holders(db, event.id)),
    }


def _ticket_holders(db: Session, event_id: uuid.UUID) -> set:
    holders = set(db.execute(
        select(Ticket.holder_id).where(Ticket.event_id == event_id,
                                       Ticket.status.in_(LIVE_TICKET_STATUSES))
    ).scalars())
    holders |= set(db.execute(
        select(EventRsvp.user_id).where(EventRsvp.event_id == event_id,
                                        EventRsvp.status == RSVP_GOING)
    ).scalars())
    return holders


# --- cancel -------------------------------------------------------------------------------------

def cancel_tickets(db: Session, event: Event, message: Optional[str]) -> dict:
    """The ticketing half of a cancellation, inside the caller's transaction (which has just
    moved the event to cancelled). Returns what to report; `after_cancel` does the rest once
    it has committed."""
    from src.modules.events import ticket_refunds, waitlist_service
    from src.modules.payments import money
    from src.modules.wallet import wallet_service

    guests = _ticket_holders(db, event.id)
    orders = _paid_orders(db, event.id, lock=True)
    fees = sum(stripe_fee_for(db, o) for o in orders)
    refund_total = sum(o.total_cents - o.refunded_cents for o in orders)

    voided = ticket_refunds.void_unrefunded(db, event)
    if voided:
        wallet_service.tickets_changed(db, voided)

    # Free orders have nothing to refund; they're simply off.
    for order in db.execute(
        select(TicketOrder).where(TicketOrder.event_id == event.id,
                                  TicketOrder.status == ORDER_PAID,
                                  TicketOrder.total_cents == 0).with_for_update()
    ).scalars():
        order.status = ORDER_CANCELLED
    # Unpaid checkouts: called off now, their payments cancelled after the commit. A payment
    # that slips through anyway is refunded by the webhook (the event isn't on any more).
    pending = []
    for order in db.execute(
        select(TicketOrder).where(TicketOrder.event_id == event.id,
                                  TicketOrder.status == ORDER_PENDING).with_for_update()
    ).scalars():
        order.status = ORDER_CANCELLED
        if order.payment_reference:
            pending.append(order.payment_reference)

    waiting = waitlist_service.close_for_event(db, event)

    debt = None
    if fees > 0:
        debt = db.execute(select(HostFeeDebt).where(HostFeeDebt.event_id == event.id)).scalar_one_or_none()
        if debt is None:
            debt = HostFeeDebt(id=uuid.uuid4(), host_id=event.host_id, event_id=event.id,
                               amount_cents=fees, status=DEBT_OPEN,
                               note="Card fees on refunds after the host cancelled.")
            db.add(debt)
            db.flush()
            money.book_host_fee_debt(db, debt, commit=False)

    return {
        "ticketsVoided": len(voided),
        "ordersToRefund": [str(o.id) for o in orders],
        "refundTotalCents": refund_total,
        "cardFeesCents": fees,
        "waitlistCleared": len(waiting),
        "_pendingIntents": pending,
        "_notify": sorted(str(u) for u in (guests | set(waiting)) if u != event.host_id),
        "_message": message,
    }


def after_cancel(db: Session, event: Event, outcome: dict) -> None:
    """After the cancellation commits: queue the refunds, cancel open payments, tell people."""
    from src.jobs.dispatch import enqueue

    for intent_id in outcome.pop("_pendingIntents", []):
        enqueue("src.modules.events.event_changes.cancel_intent", intent_id)
    for order_id in outcome["ordersToRefund"]:
        enqueue("src.modules.events.ticket_refunds.refund_for_cancelled_event", order_id)
    people = outcome.pop("_notify", [])
    message = outcome.pop("_message", None)
    if people:
        enqueue("src.modules.events.event_changes.notify_cancelled", str(event.id),
                ",".join(people), message or "")
    outcome["ordersToRefund"] = len(outcome["ordersToRefund"])


def cancel_intent(intent_id: str) -> None:
    if not stripe_configured() or intent_id.startswith("dev"):
        return
    try:
        get_stripe().PaymentIntent.cancel(intent_id)
    except Exception as exc:  # noqa: BLE001 — a payment that went through is refunded by the webhook
        logger.info("Could not cancel ticket payment %s after the event was cancelled: %s",
                    intent_id, exc)


def notify_cancelled(event_id: str, user_ids_csv: str, message: str = "") -> None:
    """Spec: "Notify guests and waitlist with the optional message" — push and email."""
    from src.shared.config.database import SessionLocal
    from src.shared.models.user import User
    from src.shared.notification.email_service import send_email
    from src.shared.templates.event_emails import simple_event_email

    db = SessionLocal()
    try:
        event = db.get(Event, uuid.UUID(event_id))
        if event is None:
            return
        body = f'"{event.title}" has been cancelled.'
        lines = [body, "If you bought tickets, you're being refunded in full automatically. "
                       "It may take a few days to appear."]
        for raw in user_ids_csv.split(","):
            if not raw:
                continue
            user_id = uuid.UUID(raw)
            _push(db, user_id, type_="event_cancelled", event=event, title="Event cancelled",
                  body=f"{body} {message}".strip() if message else
                  f"{body} Tickets are refunded in full.")
            user = db.get(User, user_id)
            if user is not None and user.email:
                send_email(user.email, f"Cancelled: {event.title}",
                           simple_event_email(event, "Event cancelled", lines,
                                              host_message=message or None))
    finally:
        db.close()


# --- reschedule ---------------------------------------------------------------------------------

def reschedule(db: Session, event: Event, starts_at: datetime, ends_at: datetime,
               message: Optional[str]) -> dict:
    """Move a published event. Caller commits; `after_reschedule` tells people."""
    from src.modules.bids import auction_dao
    from src.modules.bids.auction_dao import EVENT_CLOSE_BUFFER
    from src.modules.events import events_dao
    from src.modules.wallet import wallet_service
    from src.shared.models.bid import Bid

    if event.status != EVENT_PUBLISHED:
        raise AppError("Only a published event can be rescheduled.", 409)
    now = _now()
    if event.ends_at <= now:
        raise AppError("This event has already ended.", 409)
    events_dao.validate_window(starts_at, ends_at)
    if starts_at <= now:
        raise AppError("The new start has to be in the future.", 400)
    if starts_at == event.starts_at and ends_at == event.ends_at:
        raise AppError("That's the time it's already at.", 400)

    moved_auctions = 0
    for entry in db.execute(
        select(EventPiece).where(EventPiece.event_id == event.id, EventPiece.mode == PIECE_BID)
    ).scalars():
        auction = auction_dao.get_running_auction(db, entry.piece_id, lock=True)
        if auction is None:
            continue
        bids = db.execute(select(func.count(Bid.id)).where(Bid.auction_id == auction.id)).scalar_one()
        if bids:
            raise AppError(
                "Bidding has started on a piece at this event, so it can't move. Cancel the "
                "event instead, or wait until the auction closes.", 409,
            )
        closes_at = _new_auction_end(event, starts_at, ends_at) or (ends_at - EVENT_CLOSE_BUFFER)
        if closes_at <= starts_at:
            raise AppError("The new time is too short for the event's auction: bidding closes "
                           "30 minutes before the end.", 400)
        auction.opens_at = starts_at
        auction.closes_at = closes_at
        moved_auctions += 1

    event.auction_ends_at = _new_auction_end(event, starts_at, ends_at)
    event.starts_at = starts_at
    event.ends_at = ends_at
    event.rescheduled_at = now
    event.reschedule_message = _clean_message(message)
    # The day-before and 2-hour reminders go out again for the new date.
    for rsvp in db.execute(select(EventRsvp).where(EventRsvp.event_id == event.id)).scalars():
        rsvp.reminded_at = None
        rsvp.reminded_soon_at = None
    live = db.execute(
        select(Ticket.id).where(Ticket.event_id == event.id, Ticket.status.in_(LIVE_TICKET_STATUSES))
    ).scalars().all()
    wallet_service.event_changed(db, event, live)
    return {"movedAuctions": moved_auctions, "guestsToNotify": len(_ticket_holders(db, event.id))}


def _new_auction_end(event: Event, starts_at: datetime, ends_at: datetime):
    """The host's bidding end moves with the event, keeping its distance from the end; if
    that no longer fits the new window, it falls back to the default (30 minutes before)."""
    from src.modules.bids.auction_dao import EVENT_CLOSE_BUFFER

    if event.auction_ends_at is None:
        return None
    moved = event.auction_ends_at + (ends_at - event.ends_at)
    if moved <= starts_at or moved > ends_at - EVENT_CLOSE_BUFFER:
        return None
    return moved


def after_reschedule(db: Session, event: Event) -> None:
    from src.jobs.dispatch import enqueue

    people = sorted(str(u) for u in _ticket_holders(db, event.id) if u != event.host_id)
    if people:
        enqueue("src.modules.events.event_changes.notify_rescheduled", str(event.id),
                event.rescheduled_at.isoformat(), ",".join(people))


def notify_rescheduled(event_id: str, rescheduled_at: str, user_ids_csv: str) -> None:
    """Every ticket holder, once per reschedule: push and email (spec)."""
    from src.shared.config.database import SessionLocal
    from src.shared.models.user import User
    from src.shared.notification.email_service import send_email
    from src.shared.templates.event_emails import simple_event_email
    from src.shared.templates.ticket_email import local_time_label

    db = SessionLocal()
    try:
        event = db.get(Event, uuid.UUID(event_id))
        if event is None or event.rescheduled_at is None:
            return
        if event.rescheduled_at.isoformat() != rescheduled_at:
            return  # moved again since: that reschedule sends its own notice
        when = local_time_label(event)
        body = f"{event.title} has moved to {when}. Your tickets still work."
        lines = [body, "Can't make the new time? You can get a full refund from your ticket "
                       "until the event starts."]
        for raw in user_ids_csv.split(","):
            if not raw:
                continue
            user_id = uuid.UUID(raw)
            _push(db, user_id, type_="event_rescheduled", event=event, title="Event rescheduled",
                  body=body, payload={"startsAt": event.starts_at.isoformat()})
            user = db.get(User, user_id)
            if user is not None and user.email:
                send_email(user.email, f"New time: {event.title}",
                           simple_event_email(event, "Event rescheduled", lines,
                                              host_message=event.reschedule_message))
    finally:
        db.close()


def _push(db, user_id, *, type_, event, title, body, payload=None) -> None:
    from src.modules.notifications import notifications_dao

    try:
        notifications_dao.create_and_push(
            db, user_id=user_id, type=type_, target_type="event", target_id=event.id,
            title=title, body=body, payload=payload or {},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Event notification %s failed for %s", type_, user_id)

