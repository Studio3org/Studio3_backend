"""Refunding event tickets (ticketing spec, "Refunds, rescheduling and cancelling").

Two ways in:

* **The buyer**, inside the event's refund window — 7 days before, 24 hours before, or never;
  any time before the start once the host has rescheduled. Any of their tickets that haven't
  been checked in, one at a time or all together. The tickets' QR codes stop working and the
  tickets go to the waitlist first, then back on sale.
* **The host cancelling**: every paid order refunded in full, in the background
  (event_changes.cancel_event queues one job per order).

The money follows refunds_service's three steps, because Stripe sits in the middle and is
never called under a lock: record the refund (one in flight per order), ask Stripe for that
amount, then mark the tickets, the order and the ledger. A Stripe failure leaves the refund
`failed` with the tickets untouched, for the admin console's retry; running this again picks
the same refund up, so it can't pay twice.

Amounts: a part refund returns the tickets' prices plus their share of tax; the refund that
finishes an order returns exactly what is left, so rounding a ticket at a time never loses a
cent. Studio 3's fee on refunded tickets goes back to the host's side of the ledger, and the
Stripe fee stays a cost (for the host, when they cancel: event_changes).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from src.shared.config import commission as commission_policy
from src.shared.config.database import SessionLocal
from src.shared.config.stripe_client import get_stripe, stripe_configured
from src.shared.config.tax import tax_cents
from src.shared.models.event import (
    EVENT_CANCELLED,
    EVENT_PUBLISHED,
    REFUND_POLICY_24H,
    REFUND_POLICY_7D,
    RSVP_CANCELLED,
    Event,
    EventRsvp,
)
from src.shared.models.refund import (
    LEG_TICKETS,
    REFUND_FAILED,
    REFUND_PENDING,
    REFUND_SUCCEEDED,
    ROLE_ADMIN,
    ROLE_BUYER,
    ROLE_HOST,
    Refund,
)
from src.shared.models.ticketing import (
    LIVE_TICKET_STATUSES,
    ORDER_PAID,
    ORDER_PARTLY_REFUNDED,
    ORDER_REFUNDED,
    TICKET_CHECKED_IN,
    TICKET_REFUNDED,
    TICKET_VALID,
    TICKET_VOID,
    Ticket,
    TicketOrder,
    TicketOrderItem,
)
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

REFUNDABLE_ORDER_STATUSES = (ORDER_PAID, ORDER_PARTLY_REFUNDED)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- the window ---------------------------------------------------------------------------------

def refundable_until(event: Event) -> Optional[datetime]:
    """When the buyer's refund button goes away, or None for never (spec)."""
    if event.status != EVENT_PUBLISHED:
        return None
    if event.rescheduled_at is not None:
        # "Anyone can get a full refund until the new event starts, even if the refund policy
        # said otherwise."
        return event.starts_at
    if event.refund_policy == REFUND_POLICY_7D:
        return event.starts_at - timedelta(days=7)
    if event.refund_policy == REFUND_POLICY_24H:
        return event.starts_at - timedelta(hours=24)
    return None


def buyer_can_refund(event: Event, now: Optional[datetime] = None) -> bool:
    until = refundable_until(event)
    return until is not None and (now or _now()) < until


def ticket_refundable(event: Event, ticket: Ticket, order: TicketOrder) -> bool:
    return (
        ticket.status == TICKET_VALID
        and order.total_cents > 0
        and order.status in REFUNDABLE_ORDER_STATUSES
        and buyer_can_refund(event)
    )


# --- amounts -------------------------------------------------------------------------------------

def _unit_prices(db, order: TicketOrder) -> dict:
    return {
        item.ticket_type_id: item.unit_price_cents
        for item in db.execute(
            select(TicketOrderItem).where(TicketOrderItem.order_id == order.id)
        ).scalars()
    }


def _split(db, order: TicketOrder, tickets: list[Ticket]) -> tuple[int, int, int]:
    """(subtotal, tax, Studio 3 fee) that refunding `tickets` returns."""
    prices = _unit_prices(db, order)
    unrefunded = db.execute(
        select(Ticket.id).where(Ticket.order_id == order.id, Ticket.status != TICKET_REFUNDED)
    ).scalars().all()
    finishing = {t.id for t in tickets} >= set(unrefunded)
    if finishing:
        subtotal = order.subtotal_cents - order.refunded_subtotal_cents
        tax = order.tax_cents - order.refunded_tax_cents
        fee = (commission_policy.commission_cents(order.subtotal_cents, order.fee_bps)
               - order.refunded_fee_cents)
        return subtotal, tax, fee
    subtotal = sum(prices.get(t.ticket_type_id, 0) for t in tickets)
    tax = min(tax_cents(subtotal), order.tax_cents - order.refunded_tax_cents)
    fee = commission_policy.commission_cents(subtotal, order.fee_bps)
    return subtotal, tax, fee


# --- the refund itself --------------------------------------------------------------------------

def refund_tickets(
    order_id: uuid.UUID,
    *,
    actor_id: Optional[uuid.UUID],
    role: str,
    ticket_ids: Optional[list] = None,
    reason: str = "",
) -> dict:
    """Refund some or all of a ticket order. Returns {status, amountCents, ticketIds}.

    `role` is `buyer` (their own order, inside the window, tickets not yet used), `host` (the
    event was cancelled: everything left, checked in or not) or `admin` (a retry or a
    one-off, anything left). Safe to call again after a failure."""
    # --- step 1: decide and record ----------------------------------------------------------
    db = SessionLocal()
    try:
        order = db.execute(
            select(TicketOrder).where(TicketOrder.id == order_id).with_for_update()
        ).scalar_one_or_none()
        if order is None:
            raise AppError("Order not found.", 404)
        event = db.get(Event, order.event_id)
        if role == ROLE_BUYER and order.buyer_id != actor_id:
            raise AppError("Order not found.", 404)

        in_flight = db.execute(
            select(Refund).where(Refund.ticket_order_id == order.id,
                                 Refund.status.in_((REFUND_PENDING, REFUND_FAILED)))
        ).scalars().first()
        if in_flight is not None:
            if role == ROLE_BUYER and in_flight.status == REFUND_PENDING:
                raise AppError("A refund for this order is already going through.", 409)
            refund = in_flight
            refund.status = REFUND_PENDING
        else:
            if order.status not in REFUNDABLE_ORDER_STATUSES:
                if order.status == ORDER_REFUNDED:
                    return {"status": "already_refunded", "amountCents": 0, "ticketIds": []}
                raise AppError("These tickets can't be refunded.", 409)
            tickets = _pick_tickets(db, event, order, role, ticket_ids)
            subtotal, tax, fee = _split(db, order, tickets)
            refund = Refund(
                id=uuid.uuid4(),
                ticket_order_id=order.id,
                payment_intent_id=order.payment_reference or f"dev:{order.id}",
                refund_key=f"tickets:{uuid.uuid4()}",
                leg=LEG_TICKETS,
                amount_cents=subtotal + tax,
                ticket_ids=[str(t.id) for t in tickets],
                initiated_by_id=actor_id,
                initiated_by_role=role,
                reason=reason or None,
                status=REFUND_PENDING,
            )
            db.add(refund)
        refund_id = refund.id
        db.commit()
    finally:
        db.close()

    # --- step 2: Stripe, with no lock held ----------------------------------------------------
    failure = _send(refund_id)
    if failure:
        raise AppError(
            "The refund didn't go through at Stripe. We'll retry it; nothing more to do. "
            f"({failure})", 502,
        )

    # --- step 3: the tickets, the order and the books follow the money -----------------------
    return _finish(refund_id)


def _pick_tickets(db, event: Event, order: TicketOrder, role: str, ticket_ids) -> list[Ticket]:
    stmt = select(Ticket).where(Ticket.order_id == order.id).order_by(Ticket.created_at, Ticket.number)
    tickets = list(db.execute(stmt.with_for_update()).scalars())
    if ticket_ids:
        wanted = set()
        for raw in ticket_ids:
            try:
                wanted.add(uuid.UUID(str(raw)))
            except ValueError:
                raise AppError("Unknown ticket.", 400) from None
        chosen = [t for t in tickets if t.id in wanted]
        if len(chosen) != len(wanted):
            raise AppError("Those tickets aren't in this order.", 404)
    else:
        chosen = None

    if role == ROLE_BUYER:
        if order.total_cents == 0:
            raise AppError("Free tickets aren't refunded; tap Not going to give them back.", 409)
        if event.status == EVENT_CANCELLED:
            raise AppError("This event was cancelled, so everyone is refunded automatically.", 409)
        if not buyer_can_refund(event):
            raise AppError("Refunds for this event have closed.", 409)
        pool = chosen if chosen is not None else [t for t in tickets if t.status == TICKET_VALID]
        if any(t.status == TICKET_CHECKED_IN for t in pool):
            raise AppError("A ticket that's been checked in can't be refunded.", 409)
        if any(t.status != TICKET_VALID for t in pool):
            raise AppError("Some of those tickets were already refunded.", 409)
    else:
        pool = chosen if chosen is not None else [t for t in tickets if t.status != TICKET_REFUNDED]
        pool = [t for t in pool if t.status != TICKET_REFUNDED]
    if not pool:
        raise AppError("There's nothing left to refund on this order.", 409)
    return pool


def _send(refund_id: uuid.UUID) -> Optional[str]:
    db = SessionLocal()
    try:
        refund = db.get(Refund, refund_id)
        if refund.status == REFUND_SUCCEEDED:
            return None
        from src.modules.payments.refunds_service import _already_refunded, _is_dev_reference

        if refund.amount_cents <= 0 or not stripe_configured() or _is_dev_reference(refund.payment_intent_id):
            refund.status = REFUND_SUCCEEDED
            refund.failure_reason = None
            db.commit()
            return None
        try:
            result = get_stripe().Refund.create(
                payment_intent=refund.payment_intent_id,
                amount=refund.amount_cents,
                reason="requested_by_customer",
                metadata={"ticket_order_id": str(refund.ticket_order_id), "refund_id": str(refund.id),
                          "initiated_by": refund.initiated_by_role},
                idempotency_key=f"ticket_refund:{refund.id}",
            )
            refund.stripe_refund_id = result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
            refund.status = REFUND_SUCCEEDED
            refund.failure_reason = None
        except Exception as exc:  # noqa: BLE001 — anything Stripe raises is a failed refund
            if _already_refunded(exc):
                refund.status = REFUND_SUCCEEDED
                refund.failure_reason = None
            else:
                logger.exception("Ticket refund %s failed at Stripe", refund.id)
                refund.status = REFUND_FAILED
                refund.failure_reason = str(exc)[:2000]
        db.commit()
        return refund.failure_reason if refund.status == REFUND_FAILED else None
    finally:
        db.close()


def _finish(refund_id: uuid.UUID) -> dict:
    from src.modules.events import waitlist_service
    from src.modules.payments import money
    from src.modules.wallet import wallet_service

    db = SessionLocal()
    try:
        refund = db.get(Refund, refund_id)
        order = db.execute(
            select(TicketOrder).where(TicketOrder.id == refund.ticket_order_id).with_for_update()
        ).scalar_one()
        event = db.get(Event, order.event_id)
        ids = [uuid.UUID(t) for t in (refund.ticket_ids or [])]
        tickets = list(db.execute(
            select(Ticket).where(Ticket.id.in_(ids)).with_for_update()
        ).scalars())
        pending = [t for t in tickets if t.status != TICKET_REFUNDED]
        if pending:
            subtotal, tax, fee = _split(db, order, pending)
            for ticket in pending:
                ticket.status = TICKET_REFUNDED
            order.refunded_subtotal_cents += subtotal
            order.refunded_tax_cents += tax
            order.refunded_fee_cents += fee
            db.flush()  # the session doesn't autoflush; the count below must see these
            left = db.execute(
                select(Ticket.id).where(Ticket.order_id == order.id, Ticket.status != TICKET_REFUNDED)
            ).first()
            order.status = ORDER_PARTLY_REFUNDED if left else ORDER_REFUNDED
            if subtotal + tax > 0:
                money.book_ticket_refund(
                    db, order, event.host_id, refund.id,
                    subtotal_cents=subtotal, tax_cents=tax, fee_cents=fee, commit=False,
                )
            wallet_service.tickets_changed(db, [t.id for t in pending])
            _update_going(db, event, order.buyer_id)
            db.flush()
            # Spec: "release the ticket to the waitlist (or back on sale if no one is waiting)".
            if event.status == EVENT_PUBLISHED:
                waitlist_service.allocate(db, event, {t.ticket_type_id for t in pending})
        db.commit()
        if pending:
            _notify_refunded(db, event, order, refund)
        return {"status": order.status, "amountCents": refund.amount_cents,
                "ticketIds": [str(t.id) for t in tickets]}
    finally:
        db.close()


def _update_going(db, event: Event, user_id: uuid.UUID) -> None:
    """Someone with no admission left isn't going any more (reminders, guest list)."""
    live = db.execute(
        select(Ticket.id).where(Ticket.event_id == event.id, Ticket.holder_id == user_id,
                                Ticket.status.in_(LIVE_TICKET_STATUSES))
    ).first()
    if live:
        return
    rsvp = db.execute(
        select(EventRsvp).where(EventRsvp.event_id == event.id, EventRsvp.user_id == user_id)
    ).scalar_one_or_none()
    if rsvp is not None and rsvp.status != RSVP_CANCELLED:
        rsvp.status = RSVP_CANCELLED
        rsvp.cancelled_at = _now()


def _notify_refunded(db, event: Event, order: TicketOrder, refund: Refund) -> None:
    if refund.initiated_by_role == ROLE_HOST:
        return  # the cancellation notice already says everyone is refunded
    from src.modules.notifications import notifications_dao

    amount = f"${refund.amount_cents / 100:,.2f}"
    try:
        notifications_dao.create_and_push(
            db, user_id=order.buyer_id, type="ticket_refunded", target_type="event",
            target_id=event.id, title="Refund issued",
            body=f"We've refunded {amount} for your tickets to {event.title}. "
                 "It may take a few days to appear.",
            payload={"orderId": str(order.id), "amountCents": refund.amount_cents},
        )
    except Exception:  # noqa: BLE001
        logger.warning("Refund notification failed for ticket order %s", order.id)


# --- background ---------------------------------------------------------------------------------

def refund_for_cancelled_event(order_id: str) -> None:
    """One paid order of a cancelled event, refunded in full. Queued by cancel_event; safe to
    run twice. A Stripe failure is left `failed` for the admin console's retry."""
    try:
        refund_tickets(uuid.UUID(order_id), actor_id=None, role=ROLE_HOST,
                       reason="The host cancelled the event.")
    except AppError as exc:
        logger.warning("Cancelled-event refund for ticket order %s: %s", order_id, exc.message)


def retry(refund_id: uuid.UUID, admin_id: uuid.UUID) -> dict:
    """The admin console's Retry for a ticket refund that failed at Stripe."""
    db = SessionLocal()
    try:
        refund = db.get(Refund, refund_id)
        if refund is None or refund.ticket_order_id is None:
            raise AppError("Refund not found.", 404)
        order_id = refund.ticket_order_id
    finally:
        db.close()
    return refund_tickets(order_id, actor_id=admin_id, role=ROLE_ADMIN, reason="Retried by admin")


def void_unrefunded(db, event: Event) -> list[uuid.UUID]:
    """Cancelling: every admission that still works stops working now; the money follows in
    the background. Returns the ticket ids voided."""
    tickets = list(db.execute(
        select(Ticket).where(Ticket.event_id == event.id,
                             Ticket.status.in_(LIVE_TICKET_STATUSES)).with_for_update()
    ).scalars())
    for ticket in tickets:
        ticket.status = TICKET_VOID
    return [t.id for t in tickets]
