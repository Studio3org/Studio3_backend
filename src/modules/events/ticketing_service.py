"""Selling event tickets: types, stock, the checkout hold, issuing tickets, and money in.

Built to the ticketing spec of 25 Oct 2026 (BACKEND_CHANGES B22). The rules that matter:

* **Stock is exact.** A ticket type's stock is its quantity minus the tickets that still admit
  someone and the tickets held in unpaid checkouts. It is counted under a row lock on the
  ticket types being bought, so two people cannot both get the last one.
* **The hold.** Starting checkout holds the chosen tickets for 10 minutes. Paying in time
  issues them; otherwise `expire_holds` (every minute) releases them — after Stripe agrees
  nothing was paid, the same arbitration the art-order sweep uses.
* **Buyers pay prices plus tax, nothing else.** Studio 3's 8% comes out of the host's share,
  at the rate snapshotted on the order (settings PLATFORM_TICKET_COMMISSION_BPS).
* **Free tickets are real tickets.** A free order, or a plain RSVP, issues tickets with QR
  codes straight away (spec: "Reserve creates the tickets right away, same QR codes").
* **Attendance follows tickets.** Holding a ticket marks you `going` (event_rsvps), so the
  reminders, the guest list and "Your events" — all built on RSVPs — include ticket holders
  without each learning about tickets.
* **Stripe is never called inside a lock.** The order and its hold are committed first, then
  the PaymentIntent is created; the webhook issues the tickets.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.shared.config import commission as commission_policy
from src.shared.config.stripe_client import get_stripe, platform_currency, stripe_configured
from src.shared.config.tax import tax_cents
from src.shared.models.event import (
    EVENT_PUBLISHED,
    REFUND_POLICY_24H,
    REFUND_POLICY_7D,
    RSVP_CANCELLED,
    RSVP_GOING,
    Event,
    EventRsvp,
)
from src.shared.models.ticketing import (
    LIVE_TICKET_STATUSES,
    ORDER_EXPIRED,
    ORDER_FAILED,
    ORDER_KIND_PURCHASE,
    ORDER_KIND_RSVP,
    ORDER_PAID,
    ORDER_PENDING,
    RSVP_TICKET_NAME,
    TICKET_VALID,
    TICKET_VOID,
    Ticket,
    TicketOrder,
    TicketOrderItem,
    TicketType,
)
from src.shared.models.user import User
from src.shared.ticketing import ticket_codes
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

HOLD_FOR = timedelta(minutes=10)
MAX_TYPES_PER_EVENT = 20
MAX_TICKETS_PER_ORDER = 50
# Shown when stock runs low (spec: "Only 3 left" in red at 5 or fewer). The app decides how to
# draw it; the server only sends the number.
_NUMBER_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O, 1/I: read aloud at a door


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- ticket types (host) -------------------------------------------------------------------

def _int_or_none(raw, field: str, *, minimum: int) -> Optional[int]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise AppError(f"{field} must be a whole number.", 400)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise AppError(f"{field} must be a whole number.", 400) from None
    if value < minimum:
        raise AppError(f"{field} must be at least {minimum}.", 400)
    return value


def _time_or_none(raw, field: str) -> Optional[datetime]:
    if raw in (None, ""):
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        raise AppError(f"{field} must be an ISO 8601 date-time.", 400) from None
    if value.tzinfo is None:
        raise AppError(f"{field} must include a time zone offset.", 400)
    return value


def _apply_type_fields(ticket_type: TicketType, body: dict, *, creating: bool) -> None:
    if creating or "name" in body:
        name = (body.get("name") or "").strip()
        if not name:
            raise AppError("A ticket type needs a name.", 400)
        if len(name) > 80:
            raise AppError("Keep the ticket name under 80 characters.", 400)
        ticket_type.name = name
    if creating or "priceCents" in body:
        ticket_type.price_cents = _int_or_none(body.get("priceCents") or 0, "priceCents", minimum=0)
        if 0 < ticket_type.price_cents < 50:
            raise AppError("A paid ticket must be at least $0.50.", 400)
    if creating or "includes" in body:
        ticket_type.includes = (body.get("includes") or "").strip()[:1000] or None
    if creating or "quantity" in body:
        ticket_type.quantity = _int_or_none(body.get("quantity"), "quantity", minimum=1)
    if creating or "perOrderLimit" in body or "maxPerOrder" in body:
        raw = body.get("perOrderLimit", body.get("maxPerOrder"))
        ticket_type.max_per_order = _int_or_none(raw, "perOrderLimit", minimum=1)
    if creating or "saleStartsAt" in body:
        ticket_type.sale_starts_at = _time_or_none(body.get("saleStartsAt"), "saleStartsAt")
    if creating or "saleEndsAt" in body:
        ticket_type.sale_ends_at = _time_or_none(body.get("saleEndsAt"), "saleEndsAt")
    if (ticket_type.sale_starts_at and ticket_type.sale_ends_at
            and ticket_type.sale_ends_at <= ticket_type.sale_starts_at):
        raise AppError("A sale has to end after it starts.", 400)
    if "sortOrder" in body:
        ticket_type.sort_order = _int_or_none(body.get("sortOrder"), "sortOrder", minimum=0) or 0


def _require_payable_host(db: Session, event: Event, ticket_type: TicketType) -> None:
    """Paid tickets need a host we can pay (spec: "Paid ticket types need the host to have a
    seller profile. If not, the app prompts them to set one up")."""
    if ticket_type.price_cents == 0:
        return
    host = db.get(User, event.host_id)
    if host is None or not host.stripe_payouts_enabled:
        raise AppError(
            "Set up your seller profile to sell paid tickets, so we can pay you.", 403
        )


def list_types(db: Session, event_id: uuid.UUID, *, include_closed: bool = False) -> list[TicketType]:
    stmt = select(TicketType).where(TicketType.event_id == event_id)
    if not include_closed:
        stmt = stmt.where(TicketType.closed_at.is_(None))
    return list(db.execute(stmt.order_by(TicketType.sort_order, TicketType.created_at)).scalars())


def create_type(db: Session, event: Event, body: dict) -> TicketType:
    if len(list_types(db, event.id)) >= MAX_TYPES_PER_EVENT:
        raise AppError(f"An event can have at most {MAX_TYPES_PER_EVENT} ticket types.", 400)
    ticket_type = TicketType(id=uuid.uuid4(), event_id=event.id, sort_order=0)
    _apply_type_fields(ticket_type, body, creating=True)
    _require_payable_host(db, event, ticket_type)
    if "sortOrder" not in body:
        ticket_type.sort_order = len(list_types(db, event.id, include_closed=True))
    db.add(ticket_type)
    db.commit()
    db.refresh(ticket_type)
    return ticket_type


def _get_type(db: Session, event: Event, type_id: uuid.UUID, *, lock: bool = False) -> TicketType:
    stmt = select(TicketType).where(TicketType.id == type_id, TicketType.event_id == event.id)
    if lock:
        stmt = stmt.with_for_update()
    ticket_type = db.execute(stmt).scalar_one_or_none()
    if ticket_type is None:
        raise AppError("Ticket type not found.", 404)
    return ticket_type


def update_type(db: Session, event: Event, type_id: uuid.UUID, body: dict) -> TicketType:
    ticket_type = _get_type(db, event, type_id, lock=True)
    sold = _taken(db, [ticket_type.id]).get(ticket_type.id, 0)
    if sold and "priceCents" in body and int(body.get("priceCents") or 0) != ticket_type.price_cents:
        raise AppError(
            "This ticket has sold, so its price can't change. Add a new ticket type instead.", 409
        )
    _apply_type_fields(ticket_type, body, creating=False)
    if ticket_type.quantity is not None and ticket_type.quantity < sold:
        raise AppError(f"{sold} of these are already sold or held, so the quantity can't go "
                       "below that.", 409)
    _require_payable_host(db, event, ticket_type)
    db.commit()
    db.refresh(ticket_type)
    return ticket_type


def remove_type(db: Session, event: Event, type_id: uuid.UUID) -> str:
    """Delete a type nobody has bought; close one that has sold (its tickets stay valid)."""
    ticket_type = _get_type(db, event, type_id, lock=True)
    ever_sold = db.execute(
        select(func.count(TicketOrderItem.id)).where(TicketOrderItem.ticket_type_id == ticket_type.id)
    ).scalar_one()
    if ever_sold:
        ticket_type.closed_at = ticket_type.closed_at or _now()
        db.commit()
        return "closed"
    db.delete(ticket_type)
    db.commit()
    return "deleted"


# --- stock -------------------------------------------------------------------------------------

def _taken(db: Session, type_ids) -> dict:
    """{type id: tickets that still admit someone + tickets held in unpaid checkouts}."""
    ids = list(type_ids)
    if not ids:
        return {}
    issued = dict(db.execute(
        select(Ticket.ticket_type_id, func.count(Ticket.id))
        .where(Ticket.ticket_type_id.in_(ids), Ticket.status.in_(LIVE_TICKET_STATUSES))
        .group_by(Ticket.ticket_type_id)
    ).all())
    held = dict(db.execute(
        select(TicketOrderItem.ticket_type_id, func.coalesce(func.sum(TicketOrderItem.quantity), 0))
        .join(TicketOrder, TicketOrder.id == TicketOrderItem.order_id)
        .where(
            TicketOrderItem.ticket_type_id.in_(ids),
            TicketOrder.status == ORDER_PENDING,
            TicketOrder.hold_expires_at > _now(),
        )
        .group_by(TicketOrderItem.ticket_type_id)
    ).all())
    return {tid: int(issued.get(tid, 0)) + int(held.get(tid, 0)) for tid in ids}


def _left(ticket_type: TicketType, taken: dict) -> Optional[int]:
    if ticket_type.quantity is None:
        return None
    return max(0, ticket_type.quantity - taken.get(ticket_type.id, 0))


def _on_sale(ticket_type: TicketType, now: datetime) -> bool:
    if ticket_type.closed_at is not None:
        return False
    if ticket_type.sale_starts_at and now < ticket_type.sale_starts_at:
        return False
    if ticket_type.sale_ends_at and now >= ticket_type.sale_ends_at:
        return False
    return True


# --- what the event page shows -----------------------------------------------------------------

def type_to_dict(db: Session, ticket_type: TicketType, taken: dict, *, host=None) -> dict:
    left = _left(ticket_type, taken)
    data = {
        "id": str(ticket_type.id),
        "name": ticket_type.name,
        "priceCents": ticket_type.price_cents,
        "includes": ticket_type.includes,
        "left": left,
        "perOrderLimit": ticket_type.max_per_order,
        "saleStartsAt": ticket_type.sale_starts_at.isoformat() if ticket_type.sale_starts_at else None,
        "saleEndsAt": ticket_type.sale_ends_at.isoformat() if ticket_type.sale_ends_at else None,
        "onSale": _on_sale(ticket_type, _now()),
        "soldOut": left == 0,
    }
    if host is not None:
        # The host's own view (spec: "Show the host what they receive per ticket").
        bps = commission_policy.resolve_bps(host, commission_policy.SALE_TICKET)
        data.update({
            "quantity": ticket_type.quantity,
            "sold": taken.get(ticket_type.id, 0),
            "hostNetCents": commission_policy.net_cents(ticket_type.price_cents, bps),
            "closed": ticket_type.closed_at is not None,
        })
    return data


def event_ticket_fields(db: Session, event: Event, viewer_id: Optional[uuid.UUID]) -> dict:
    """`ticketTypes`, `isFree` and `ticketCount` for the event detail (B22's contract)."""
    is_host = viewer_id is not None and viewer_id == event.host_id
    types = list_types(db, event.id, include_closed=is_host)
    taken = _taken(db, [t.id for t in types])
    host = db.get(User, event.host_id) if is_host else None
    return {
        "ticketTypes": [type_to_dict(db, t, taken, host=host) for t in types],
        "isFree": not any(t.price_cents > 0 for t in types if t.closed_at is None),
        "ticketCount": viewer_ticket_count(db, event.id, viewer_id),
    }


def card_ticket_fields(db: Session, event: Event, viewer_id: Optional[uuid.UUID]) -> dict:
    """The two cheap fields every event card carries."""
    any_paid = db.execute(
        select(func.count(TicketType.id)).where(
            TicketType.event_id == event.id, TicketType.price_cents > 0,
            TicketType.closed_at.is_(None),
        )
    ).scalar_one()
    return {"isFree": any_paid == 0, "ticketCount": viewer_ticket_count(db, event.id, viewer_id)}


def viewer_ticket_count(db: Session, event_id: uuid.UUID, viewer_id: Optional[uuid.UUID]) -> int:
    if viewer_id is None:
        return 0
    return db.execute(
        select(func.count(Ticket.id)).where(
            Ticket.event_id == event_id, Ticket.holder_id == viewer_id,
            Ticket.status.in_(LIVE_TICKET_STATUSES),
        )
    ).scalar_one()


def refund_policy_line(event: Event) -> str:
    if event.refund_policy == REFUND_POLICY_7D:
        return "Refunds up to 7 days before the event."
    if event.refund_policy == REFUND_POLICY_24H:
        return "Refunds up to 24 hours before the event."
    return "No refunds."


# --- choosing tickets --------------------------------------------------------------------------

def _parse_items(raw) -> dict:
    """{ticket type id: quantity} from `[{ticketTypeId, quantity}]`, duplicates merged."""
    if not isinstance(raw, list) or not raw:
        raise AppError("Choose at least one ticket.", 400)
    wanted: dict = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise AppError("Each item needs a ticketTypeId and a quantity.", 400)
        try:
            type_id = uuid.UUID(str(entry.get("ticketTypeId")))
        except ValueError:
            raise AppError("Unknown ticket type.", 400) from None
        quantity = _int_or_none(entry.get("quantity"), "quantity", minimum=0) or 0
        wanted[type_id] = wanted.get(type_id, 0) + quantity
    wanted = {k: v for k, v in wanted.items() if v > 0}
    if not wanted:
        raise AppError("Choose at least one ticket.", 400)
    if sum(wanted.values()) > MAX_TICKETS_PER_ORDER:
        raise AppError(f"An order can have at most {MAX_TICKETS_PER_ORDER} tickets.", 400)
    return wanted


def _require_open_event(event: Event, buyer_id: Optional[uuid.UUID]) -> None:
    if event.status != EVENT_PUBLISHED:
        raise AppError("Tickets for this event aren't on sale.", 409)
    if event.ends_at <= _now():
        raise AppError("This event has already ended.", 409)
    if buyer_id is not None and buyer_id == event.host_id:
        raise AppError("You're hosting this one.", 400)


def _priced(db: Session, event: Event, wanted: dict, *, lock: bool) -> list[tuple[TicketType, int]]:
    """Check every chosen type can be sold now and return (type, quantity) pairs.

    With `lock`, the types are locked in id order (a fixed order, so two orders for the same
    types cannot deadlock) and stock is checked against holds and issued tickets."""
    stmt = select(TicketType).where(
        TicketType.id.in_(list(wanted)), TicketType.event_id == event.id
    ).order_by(TicketType.id)
    if lock:
        stmt = stmt.with_for_update()
    types = {t.id: t for t in db.execute(stmt).scalars()}
    if len(types) != len(wanted):
        raise AppError("One of those tickets isn't available for this event.", 409)
    taken = _taken(db, list(types))
    now = _now()
    picked = []
    for type_id, quantity in wanted.items():
        ticket_type = types[type_id]
        if not _on_sale(ticket_type, now):
            raise AppError(f"{ticket_type.name} isn't on sale right now.", 409)
        if ticket_type.max_per_order and quantity > ticket_type.max_per_order:
            raise AppError(
                f"You can buy up to {ticket_type.max_per_order} {ticket_type.name} per order.", 409
            )
        left = _left(ticket_type, taken)
        if left is not None and quantity > left:
            raise AppError(
                f"{ticket_type.name} is sold out." if left == 0
                else f"Only {left} {ticket_type.name} left.",
                409,
            )
        picked.append((ticket_type, quantity))
    return picked


def quote(db: Session, event: Event, raw_items, viewer_id: Optional[uuid.UUID]) -> dict:
    """Totals for Checkout. No hold — stock is checked again when the order is placed."""
    _require_open_event(event, viewer_id)
    picked = _priced(db, event, _parse_items(raw_items), lock=False)
    subtotal = sum(t.price_cents * q for t, q in picked)
    tax = tax_cents(subtotal)
    return {
        "items": [
            {"ticketTypeId": str(t.id), "name": t.name, "quantity": q,
             "unitPriceCents": t.price_cents, "lineCents": t.price_cents * q}
            for t, q in picked
        ],
        "subtotalCents": subtotal,
        # Buyers never pay a fee (spec); sent as 0 because B22's app model reads the field.
        "serviceFeeCents": 0,
        "taxCents": tax,
        "totalCents": subtotal + tax,
        "refundPolicy": refund_policy_line(event),
    }


# --- placing an order -------------------------------------------------------------------------

def place_order(db: Session, event: Event, buyer: User, raw_items, email: Optional[str]) -> dict:
    """Hold the tickets and start payment, or issue them at once when nothing is due."""
    _require_open_event(event, buyer.id)
    wanted = _parse_items(raw_items)
    host = db.get(User, event.host_id)

    # --- under the ticket-type locks: check stock and record the hold --------------------
    picked = _priced(db, event, wanted, lock=True)
    subtotal = sum(t.price_cents * q for t, q in picked)
    tax = tax_cents(subtotal)
    order = TicketOrder(
        id=uuid.uuid4(),
        event_id=event.id,
        buyer_id=buyer.id,
        kind=ORDER_KIND_PURCHASE,
        email=(email or buyer.email or "").strip()[:255] or None,
        subtotal_cents=subtotal,
        tax_cents=tax,
        total_cents=subtotal + tax,
        fee_bps=commission_policy.resolve_bps(host, commission_policy.SALE_TICKET),
        status=ORDER_PENDING,
        hold_expires_at=_now() + HOLD_FOR,
    )
    db.add(order)
    db.flush()
    for ticket_type, quantity in picked:
        db.add(TicketOrderItem(
            id=uuid.uuid4(), order_id=order.id, ticket_type_id=ticket_type.id,
            quantity=quantity, unit_price_cents=ticket_type.price_cents,
        ))
    db.flush()

    if order.total_cents == 0 or not stripe_configured():
        # Free tickets, or development without Stripe: issue now, as an art order's dev-mode
        # confirm does. Production always has Stripe (settings.py requires it).
        _issue(db, order, stripe_fee_cents=0)
        db.commit()
        _after_issue(order.id)
        return _order_response(order, client_secret=None, dev_mode=order.total_cents > 0)

    db.commit()

    # --- no lock: Stripe -----------------------------------------------------------------
    try:
        intent = get_stripe().PaymentIntent.create(
            amount=order.total_cents,
            currency=platform_currency(),
            metadata={
                "kind": "ticket_order",
                "ticket_order_id": str(order.id),
                "event_id": str(event.id),
                "buyer_id": str(buyer.id),
            },
            automatic_payment_methods={"enabled": True},
            receipt_email=order.email,
            idempotency_key=f"ticket_pi:{order.id}",
        )
    except Exception as exc:  # noqa: BLE001 — any Stripe failure releases the hold
        logger.exception("Could not start payment for ticket order %s", order.id)
        order = db.get(TicketOrder, order.id)
        order.status = ORDER_FAILED
        db.commit()
        raise AppError("We couldn't start the payment. Please try again.", 502) from exc

    order = db.get(TicketOrder, order.id)
    order.payment_reference = intent["id"]
    db.commit()
    return _order_response(order, client_secret=intent["client_secret"])


def _order_response(order: TicketOrder, *, client_secret: Optional[str], dev_mode: bool = False) -> dict:
    return {
        "id": str(order.id),
        "orderId": str(order.id),
        "clientSecret": client_secret,
        "status": order.status,
        "totalCents": order.total_cents,
        "holdExpiresAt": order.hold_expires_at.isoformat() if order.hold_expires_at else None,
        **({"devMode": True} if dev_mode else {}),
    }


def order_to_dict(db: Session, order: TicketOrder) -> dict:
    tickets = db.execute(
        select(Ticket).where(Ticket.order_id == order.id).order_by(Ticket.created_at, Ticket.number)
    ).scalars().all()
    return {
        "id": str(order.id),
        "eventId": str(order.event_id),
        "status": order.status,
        "subtotalCents": order.subtotal_cents,
        "taxCents": order.tax_cents,
        "totalCents": order.total_cents,
        "email": order.email,
        "holdExpiresAt": order.hold_expires_at.isoformat() if order.hold_expires_at else None,
        "paidAt": order.paid_at.isoformat() if order.paid_at else None,
        "ticketCount": sum(1 for t in tickets if t.status in LIVE_TICKET_STATUSES),
        "createdAt": order.created_at.isoformat(),
    }


# --- issuing tickets ---------------------------------------------------------------------------

def _new_number(db: Session) -> str:
    for _ in range(10):
        number = "S3-T-" + "".join(secrets.choice(_NUMBER_ALPHABET) for _ in range(6))
        if not db.execute(select(Ticket.id).where(Ticket.number == number)).first():
            return number
    raise RuntimeError("Could not mint a unique ticket number.")


def _mark_going(db: Session, event_id: uuid.UUID, user_id: uuid.UUID) -> None:
    rsvp = db.execute(
        select(EventRsvp).where(EventRsvp.event_id == event_id, EventRsvp.user_id == user_id)
    ).scalar_one_or_none()
    if rsvp is None:
        db.add(EventRsvp(id=uuid.uuid4(), event_id=event_id, user_id=user_id, status=RSVP_GOING))
    elif rsvp.status != RSVP_GOING:
        rsvp.status = RSVP_GOING
        rsvp.cancelled_at = None


def _issue(db: Session, order: TicketOrder, *, stripe_fee_cents: int, charge_id: Optional[str] = None) -> None:
    """Turn a held order into tickets. Caller holds the order lock and commits."""
    if order.status == ORDER_PAID:
        return
    items = db.execute(select(TicketOrderItem).where(TicketOrderItem.order_id == order.id)).scalars()
    for item in items:
        for _ in range(item.quantity):
            db.add(Ticket(
                id=uuid.uuid4(), order_id=order.id, event_id=order.event_id,
                ticket_type_id=item.ticket_type_id, holder_id=order.buyer_id,
                number=_new_number(db), status=TICKET_VALID,
            ))
            db.flush()
    order.status = ORDER_PAID
    order.paid_at = _now()
    if charge_id:
        order.stripe_charge_id = charge_id
    _mark_going(db, order.event_id, order.buyer_id)
    if order.total_cents > 0:
        from src.modules.payments import money

        event = db.get(Event, order.event_id)
        money.book_ticket_order_paid(db, order, event.host_id, stripe_fee_cents, commit=False)


def _after_issue(order_id: uuid.UUID) -> None:
    """After the commit: the buyer's confirmation, in the background."""
    from src.jobs.dispatch import enqueue

    enqueue("src.modules.events.ticketing_service.send_confirmation", str(order_id))


# --- money in, from the Stripe webhook --------------------------------------------------------

def owns_intent(intent: dict) -> bool:
    metadata = intent.get("metadata") or {}
    return metadata.get("kind") == "ticket_order" or bool(metadata.get("ticket_order_id"))


def _order_for_intent(db: Session, intent: dict) -> Optional[TicketOrder]:
    raw = (intent.get("metadata") or {}).get("ticket_order_id")
    stmt = None
    if raw:
        try:
            stmt = select(TicketOrder).where(TicketOrder.id == uuid.UUID(raw))
        except ValueError:
            stmt = None
    if stmt is None:
        stmt = select(TicketOrder).where(TicketOrder.payment_reference == intent["id"])
    return db.execute(stmt.with_for_update()).scalar_one_or_none()


def on_payment_succeeded(db: Session, intent: dict, *, stripe_fee_cents: int,
                         charge_id: Optional[str]) -> None:
    order = _order_for_intent(db, intent)
    if order is None:
        logger.error("payment_intent.succeeded for unknown ticket order (%s)", intent["id"])
        return
    if order.status == ORDER_PAID:
        return
    order.payment_reference = order.payment_reference or intent["id"]
    if order.status != ORDER_PENDING:
        # The hold ran out (or the order failed) before the money arrived. If the tickets are
        # still there, the buyer gets them; if someone else has them now, the money goes back.
        if not _still_available(db, order):
            from src.modules.payments import refunds_service

            refunds_service.refund_late_ticket_payment(db, order, intent, stripe_fee_cents)
            db.commit()
            return
    _issue(db, order, stripe_fee_cents=stripe_fee_cents, charge_id=charge_id)
    db.commit()
    _after_issue(order.id)


def _still_available(db: Session, order: TicketOrder) -> bool:
    items = list(db.execute(select(TicketOrderItem).where(TicketOrderItem.order_id == order.id)).scalars())
    type_ids = sorted({i.ticket_type_id for i in items if i.ticket_type_id})
    types = {t.id: t for t in db.execute(
        select(TicketType).where(TicketType.id.in_(type_ids)).order_by(TicketType.id).with_for_update()
    ).scalars()}
    taken = _taken(db, type_ids)
    for item in items:
        ticket_type = types.get(item.ticket_type_id)
        if ticket_type is None:
            return False
        left = _left(ticket_type, taken)
        if left is not None and item.quantity > left:
            return False
    return True


def on_payment_failed(db: Session, intent: dict) -> None:
    order = _order_for_intent(db, intent)
    if order is not None and order.status == ORDER_PENDING:
        order.status = ORDER_FAILED
        db.commit()


# --- the hold sweep ------------------------------------------------------------------------

def expire_holds() -> int:
    """Release unpaid checkouts past their hold. Runs every minute (events.expire_ticket_holds).

    Stripe arbitrates, as for art orders: the intent is cancelled first, and an intent Stripe
    won't cancel (processing, or just succeeded) is left for the webhook to finish."""
    from src.shared.config.database import SessionLocal

    db = SessionLocal()
    released = 0
    try:
        due = db.execute(
            select(TicketOrder.id).where(
                TicketOrder.status == ORDER_PENDING, TicketOrder.hold_expires_at <= _now()
            )
        ).scalars().all()
        for order_id in due:
            order = db.execute(
                select(TicketOrder).where(TicketOrder.id == order_id).with_for_update()
            ).scalar_one_or_none()
            if order is None or order.status != ORDER_PENDING:
                db.rollback()
                continue
            if order.payment_reference and stripe_configured():
                try:
                    get_stripe().PaymentIntent.cancel(order.payment_reference)
                except Exception as exc:  # noqa: BLE001
                    logger.info("Leaving ticket order %s held: Stripe would not cancel (%s).",
                                order.id, exc)
                    db.rollback()
                    continue
            order.status = ORDER_EXPIRED
            db.commit()
            released += 1
        return released
    finally:
        db.close()


# --- the free one-tap RSVP --------------------------------------------------------------------

def reserve(db: Session, event: Event, user: User) -> None:
    """"I'm going" — which, since tickets, also issues a free ticket with a QR code (spec).

    An event with no ticket types gets a General Admission ticket, capped by the event's own
    capacity as before. An event with ticket types gets one of its first free type; one that
    only sells paid tickets has to go through checkout.
    """
    from src.modules.events import rsvp_service

    _require_open_event(event, user.id)
    if viewer_ticket_count(db, event.id, user.id):
        _mark_going(db, event.id, user.id)
        db.commit()
        return

    types = list_types(db, event.id)
    order = TicketOrder(
        id=uuid.uuid4(), event_id=event.id, buyer_id=user.id, kind=ORDER_KIND_RSVP,
        email=user.email, subtotal_cents=0, tax_cents=0, total_cents=0, fee_bps=0,
        status=ORDER_PENDING,
    )
    if not types:
        locked = db.execute(select(Event).where(Event.id == event.id).with_for_update()).scalar_one()
        if rsvp_service.is_full(db, locked):
            raise AppError("This event is full. The host may open more places closer to the date.", 409)
        db.add(order)
        db.flush()
        db.add(TicketOrderItem(id=uuid.uuid4(), order_id=order.id, ticket_type_id=None,
                               quantity=1, unit_price_cents=0))
    else:
        free = [t for t in types if t.price_cents == 0]
        if not free:
            raise AppError("This event needs a ticket. Choose tickets to continue.", 409)
        picked = None
        for candidate in free:
            try:
                picked = _priced(db, event, {candidate.id: 1}, lock=True)
                break
            except AppError:
                continue
        if picked is None:
            raise AppError("The free tickets are gone.", 409)
        db.add(order)
        db.flush()
        db.add(TicketOrderItem(id=uuid.uuid4(), order_id=order.id,
                               ticket_type_id=picked[0][0].id, quantity=1, unit_price_cents=0))
    db.flush()
    _issue(db, order, stripe_fee_cents=0)
    db.commit()
    _after_issue(order.id)


def release(db: Session, event: Event, user: User) -> None:
    """"Not going": give back free tickets. Paid tickets are refunded, not released, so a
    buyer holding any keeps them (and stays going) — refunds arrive with Phase 4."""
    free_tickets = db.execute(
        select(Ticket).join(TicketOrder, TicketOrder.id == Ticket.order_id).where(
            Ticket.event_id == event.id, Ticket.holder_id == user.id,
            Ticket.status.in_(LIVE_TICKET_STATUSES), TicketOrder.total_cents == 0,
        )
    ).scalars().all()
    for ticket in free_tickets:
        ticket.status = TICKET_VOID
    db.flush()
    if viewer_ticket_count(db, event.id, user.id):
        db.commit()
        raise AppError(
            "Your paid tickets are still valid. You can ask for a refund from your ticket.", 409
        )
    rsvp = db.execute(
        select(EventRsvp).where(EventRsvp.event_id == event.id, EventRsvp.user_id == user.id)
    ).scalar_one_or_none()
    if rsvp is not None and rsvp.status != RSVP_CANCELLED:
        rsvp.status = RSVP_CANCELLED
        rsvp.cancelled_at = _now()
    db.commit()


# --- the buyer's tickets ---------------------------------------------------------------------

def my_tickets(db: Session, user_id: uuid.UUID, event_id: Optional[uuid.UUID]) -> list[dict]:
    stmt = (
        select(Ticket, TicketType.name)
        .outerjoin(TicketType, TicketType.id == Ticket.ticket_type_id)
        .where(Ticket.holder_id == user_id, Ticket.status.in_(LIVE_TICKET_STATUSES + (TICKET_VOID,)))
    )
    if event_id is not None:
        stmt = stmt.where(Ticket.event_id == event_id)
    rows = db.execute(stmt.order_by(Ticket.created_at, Ticket.number)).all()
    live = [(t, n) for t, n in rows if t.status in LIVE_TICKET_STATUSES]
    return [
        {
            "id": str(ticket.id),
            "eventId": str(ticket.event_id),
            "orderId": str(ticket.order_id),
            # The QR value. Signed, so the door can tell it from a forgery (ticket_codes.py).
            "code": ticket_codes.make_code(ticket.id, ticket.event_id),
            "ticketTypeName": name or RSVP_TICKET_NAME,
            "number": ticket.number,
            "status": ticket.status,
            "scannedAt": ticket.checked_in_at.isoformat() if ticket.checked_in_at else None,
        }
        for ticket, name in live
    ]


# --- the confirmation email --------------------------------------------------------------------

def send_confirmation(order_id: str) -> None:
    """Email the buyer their tickets and receipt (spec: the app, not Stripe, sends them).
    Safe to run twice; a resend is harmless."""
    from src.shared.config.database import SessionLocal
    from src.shared.notification.email_service import send_email
    from src.shared.templates.ticket_email import ticket_email_html

    db = SessionLocal()
    try:
        order = db.get(TicketOrder, uuid.UUID(str(order_id)))
        if order is None or order.status != ORDER_PAID or not order.email:
            return
        event = db.get(Event, order.event_id)
        tickets = my_tickets(db, order.buyer_id, order.event_id)
        tickets = [t for t in tickets if t["orderId"] == str(order.id)]
        subject = f"Your tickets: {event.title}" if order.total_cents else f"You're going: {event.title}"
        send_email(order.email, subject, ticket_email_html(event, order, tickets))
    finally:
        db.close()

