"""The waitlist for a sold-out event (ticketing spec, "Waitlist"; BACKEND_CHANGES B22).

How it works:

* **Joining.** Free, no card. Allowed while the types someone asks for are sold out and the
  event hasn't started; up to each type's per-order limit; for more than one type at once.
* **Order.** First come, first served by `joined_at`.
* **When tickets open up** (a refund, an unpaid checkout running out, a hold given up, the
  host adding more): they are held for the next person in line who asked for that type, up
  to the number they asked for; anything left goes to the people after them at the same
  time, and only what the whole line doesn't want goes back on sale. `allocate` does this
  under the ticket types' row locks, the same locks checkout takes, so a hold and a sale can
  never both get the last ticket.
* **The hold** lasts 2 hours, 30 minutes or 10 minutes depending on how soon the event
  starts. The person can buy (or Reserve) the held tickets, give them up and keep waiting for
  their other types, or let it run out — which removes them from the line (spec).
* **Closing.** When the event starts, or is cancelled, everyone left is removed quietly.

A hold counts against stock exactly as a checkout's does (ticketing_service._taken), and its
own holder is the one person it doesn't count against.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.shared.models.event import EVENT_PUBLISHED, Event, EventSave
from src.shared.models.ticketing import (
    HOLD_ACTIVE,
    HOLD_CANCELLED,
    HOLD_CHECKOUT,
    HOLD_EXPIRED,
    HOLD_RELEASED,
    HOLD_USED,
    LIVE_TICKET_STATUSES,
    Ticket,
    TicketOrder,
    TicketOrderItem,
    TicketType,
    WAITLIST_BOUGHT,
    WAITLIST_CLOSED,
    WAITLIST_EXPIRED,
    WAITLIST_HELD,
    WAITLIST_IN_LINE,
    WAITLIST_LEFT,
    WAITLIST_WAITING,
    WaitlistEntry,
    WaitlistHold,
)
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

# B26: a sold-out event that reopens within this long of selling out is a checkout that
# lapsed, not news — savers aren't told.
REOPEN_DEBOUNCE = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hold_length(event: Event, now: Optional[datetime] = None) -> timedelta:
    """Spec: more than 24 hours to go, 2 hours; 2 to 24 hours, 30 minutes; less, 10 minutes."""
    remaining = event.starts_at - (now or _now())
    if remaining > timedelta(hours=24):
        return timedelta(hours=2)
    if remaining >= timedelta(hours=2):
        return timedelta(minutes=30)
    return timedelta(minutes=10)


# --- stock --------------------------------------------------------------------------------

def held_counts(db: Session, type_ids, *, exclude_user: Optional[uuid.UUID] = None) -> dict:
    """{type id: tickets held for people in line}. Used by ticketing_service._taken."""
    ids = list(type_ids)
    if not ids:
        return {}
    stmt = (
        select(WaitlistHold.ticket_type_id, func.coalesce(func.sum(WaitlistHold.quantity), 0))
        .where(
            WaitlistHold.ticket_type_id.in_(ids),
            WaitlistHold.status == HOLD_ACTIVE,
            WaitlistHold.held_until > _now(),
        )
        .group_by(WaitlistHold.ticket_type_id)
    )
    if exclude_user is not None:
        stmt = stmt.where(WaitlistHold.user_id != exclude_user)
    return {tid: int(n) for tid, n in db.execute(stmt).all()}


def _types(db: Session, event_id: uuid.UUID, *, lock: bool = False, ids=None) -> list[TicketType]:
    stmt = select(TicketType).where(TicketType.event_id == event_id)
    if ids is not None:
        stmt = stmt.where(TicketType.id.in_(list(ids)))
    stmt = stmt.order_by(TicketType.id)
    if lock:
        stmt = stmt.with_for_update()
    return list(db.execute(stmt).scalars())


def _public_left(db: Session, types: list[TicketType]) -> dict:
    """{type id: left for anyone (None = no cap)}, with every hold counted."""
    from src.modules.events import ticketing_service

    taken = ticketing_service._taken(db, [t.id for t in types])
    return {t.id: ticketing_service._left(t, taken) for t in types}


def is_sold_out(db: Session, event: Event) -> bool:
    """Every type on sale has none left (spec: "Join waitlist appears when every ticket type
    is sold out"). An event with no types is full when its capacity is."""
    from src.modules.events import rsvp_service, ticketing_service

    now = _now()
    types = [t for t in _types(db, event.id) if ticketing_service._on_sale(t, now)]
    if not types:
        if _types(db, event.id):
            return False  # nothing on sale right now is not the same as sold out
        return rsvp_service.is_full(db, event)
    left = _public_left(db, types)
    return all(left[t.id] == 0 for t in types)


def _is_open(db: Session, event: Event) -> bool:
    return (
        event.status == EVENT_PUBLISHED
        and event.starts_at > _now()
        and bool(_types(db, event.id))
        and is_sold_out(db, event)
    )


# --- what the app reads ---------------------------------------------------------------------

def _entry(db: Session, event_id: uuid.UUID, user_id: uuid.UUID, *, lock: bool = False):
    stmt = select(WaitlistEntry).where(
        WaitlistEntry.event_id == event_id, WaitlistEntry.user_id == user_id
    )
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def _active_holds(db: Session, entry_id: uuid.UUID) -> list[WaitlistHold]:
    return list(db.execute(
        select(WaitlistHold).where(
            WaitlistHold.entry_id == entry_id,
            WaitlistHold.status.in_((HOLD_ACTIVE, HOLD_CHECKOUT)),
            WaitlistHold.held_until > _now(),
        ).order_by(WaitlistHold.created_at)
    ).scalars())


def _items(mapping: dict) -> list[dict]:
    return [{"ticketTypeId": str(k), "quantity": int(v)} for k, v in mapping.items() if int(v) > 0]


def waiting_by_type(db: Session, event_id: uuid.UUID) -> dict:
    """{type id (str): people in line for it} — the host's view (spec: "how many people are
    waiting and for which ticket types")."""
    counts: dict = {}
    for wanted in db.execute(
        select(WaitlistEntry.wanted).where(
            WaitlistEntry.event_id == event_id, WaitlistEntry.status.in_(WAITLIST_IN_LINE)
        )
    ).scalars():
        for type_id, quantity in (wanted or {}).items():
            if int(quantity) > 0:
                counts[type_id] = counts.get(type_id, 0) + 1
    return counts


def view(db: Session, event: Event, viewer_id: Optional[uuid.UUID]) -> Optional[dict]:
    """B22's `waitlist` object, or None when there is nothing to say (B22: "leave `waitlist`
    out while there is none")."""
    is_host = viewer_id is not None and viewer_id == event.host_id
    entry = _entry(db, event.id, viewer_id) if viewer_id and not is_host else None
    in_line = entry is not None and entry.status in WAITLIST_IN_LINE
    is_open = _is_open(db, event)
    if not (is_open or in_line or (is_host and _types(db, event.id))):
        return None
    data = {"open": is_open, "wanted": [], "held": None}
    if in_line:
        data["wanted"] = _items(entry.wanted or {})
        holds = _active_holds(db, entry.id)
        if holds:
            held: dict = {}
            for hold in holds:
                held[hold.ticket_type_id] = held.get(hold.ticket_type_id, 0) + hold.quantity
            data["held"] = {
                "items": _items(held),
                "until": min(h.held_until for h in holds).isoformat(),
            }
    if is_host:
        data["waitingCount"] = db.execute(
            select(func.count(WaitlistEntry.id)).where(
                WaitlistEntry.event_id == event.id, WaitlistEntry.status.in_(WAITLIST_IN_LINE)
            )
        ).scalar_one()
        data["waitingByType"] = waiting_by_type(db, event.id)
    return data


def card_view(db: Session, event: Event, viewer_id: Optional[uuid.UUID]) -> Optional[dict]:
    """The same, for event cards in Your events, only when the viewer is in line (one query
    for everyone else, so lists stay cheap)."""
    if viewer_id is None or viewer_id == event.host_id:
        return None
    entry = _entry(db, event.id, viewer_id)
    if entry is None or entry.status not in WAITLIST_IN_LINE:
        return None
    return view(db, event, viewer_id)


# --- joining and leaving ----------------------------------------------------------------------

def join(db: Session, event: Event, user: User, raw_items) -> dict:
    from src.modules.events import ticketing_service

    if event.status != EVENT_PUBLISHED or event.starts_at <= _now():
        raise AppError("The waitlist for this event is closed.", 409)
    if user.id == event.host_id:
        raise AppError("You're hosting this one.", 400)
    wanted = ticketing_service._parse_items(raw_items)
    types = {t.id: t for t in _types(db, event.id, ids=wanted)}
    if len(types) != len(wanted):
        raise AppError("One of those tickets isn't available for this event.", 409)
    left = _public_left(db, list(types.values()))
    for type_id, quantity in wanted.items():
        ticket_type = types[type_id]
        if ticket_type.closed_at is not None:
            raise AppError(f"{ticket_type.name} isn't on sale any more.", 409)
        if ticket_type.max_per_order and quantity > ticket_type.max_per_order:
            raise AppError(
                f"You can wait for up to {ticket_type.max_per_order} {ticket_type.name}.", 409
            )
        if left[type_id] != 0:
            raise AppError(f"{ticket_type.name} isn't sold out. Choose tickets instead.", 409)

    entry = _entry(db, event.id, user.id, lock=True)
    as_json = {str(k): int(v) for k, v in wanted.items()}
    if entry is None:
        entry = WaitlistEntry(id=uuid.uuid4(), event_id=event.id, user_id=user.id,
                              status=WAITLIST_WAITING, wanted=as_json, joined_at=_now())
        db.add(entry)
    elif entry.status in WAITLIST_IN_LINE:
        # Changing what you wait for keeps your place.
        entry.wanted = as_json
    else:
        entry.status = WAITLIST_WAITING
        entry.wanted = as_json
        entry.joined_at = _now()
    db.commit()
    return view(db, event, user.id)


def leave(db: Session, event: Event, user: User) -> None:
    entry = _entry(db, event.id, user.id, lock=True)
    if entry is None or entry.status not in WAITLIST_IN_LINE:
        return
    freed = _end_holds(db, entry, HOLD_RELEASED)
    entry.status = WAITLIST_LEFT
    db.flush()
    allocate(db, event, freed)
    db.commit()


def release_hold(db: Session, event: Event, user: User) -> dict:
    """"Keep waiting for [other type]": give up what is held, stay in line for the rest."""
    entry = _entry(db, event.id, user.id, lock=True)
    if entry is None or entry.status not in WAITLIST_IN_LINE:
        raise AppError("You're not on the waitlist for this event.", 404)
    freed = _end_holds(db, entry, HOLD_RELEASED, only_active=True)
    if not freed:
        raise AppError("Nothing is held for you right now.", 409)
    wanted = {k: v for k, v in (entry.wanted or {}).items() if uuid.UUID(k) not in freed}
    entry.wanted = wanted
    entry.status = WAITLIST_WAITING if wanted else WAITLIST_LEFT
    db.flush()
    allocate(db, event, freed)
    db.commit()
    return view(db, event, user.id) or {"open": False, "wanted": [], "held": None}


def _end_holds(db: Session, entry: WaitlistEntry, status: str, *, only_active: bool = False) -> set:
    """End an entry's live holds; returns the ticket types they freed."""
    states = (HOLD_ACTIVE,) if only_active else (HOLD_ACTIVE, HOLD_CHECKOUT)
    freed = set()
    for hold in db.execute(
        select(WaitlistHold).where(WaitlistHold.entry_id == entry.id,
                                   WaitlistHold.status.in_(states)).with_for_update()
    ).scalars():
        hold.status = status
        freed.add(hold.ticket_type_id)
    return freed


# --- handing out tickets ------------------------------------------------------------------------

def allocate(db: Session, event: Event, type_ids: Optional[Iterable] = None) -> list[uuid.UUID]:
    """Hold freed tickets for the people in line, in joining order. Runs inside the caller's
    transaction (the caller commits); people are told once it commits.

    Locks the ticket types first — the same locks checkout takes — so the count of what is
    free can't change underneath."""
    now = _now()
    if event.status != EVENT_PUBLISHED or event.starts_at <= now:
        return []
    ids = None if type_ids is None else {uuid.UUID(str(t)) for t in type_ids if t}
    if ids is not None and not ids:
        return []
    types = _types(db, event.id, lock=True, ids=ids)
    entries = list(db.execute(
        select(WaitlistEntry).where(
            WaitlistEntry.event_id == event.id, WaitlistEntry.status.in_(WAITLIST_IN_LINE)
        ).order_by(WaitlistEntry.joined_at, WaitlistEntry.id).with_for_update()
    ).scalars())
    if not entries or not types:
        note_stock(db, event)
        return []

    from src.modules.events import ticketing_service

    taken = ticketing_service._taken(db, [t.id for t in types])
    until = now + hold_length(event, now)
    created: list[uuid.UUID] = []
    for ticket_type in types:
        if not ticketing_service._on_sale(ticket_type, now):
            continue
        left = ticketing_service._left(ticket_type, taken)
        if left is None:
            continue  # no cap: nobody needs to wait
        for entry in entries:
            if left <= 0:
                break
            want = int((entry.wanted or {}).get(str(ticket_type.id), 0))
            if want <= 0:
                continue
            already = db.execute(
                select(func.count(WaitlistHold.id)).where(
                    WaitlistHold.entry_id == entry.id,
                    WaitlistHold.ticket_type_id == ticket_type.id,
                    WaitlistHold.status.in_((HOLD_ACTIVE, HOLD_CHECKOUT)),
                )
            ).scalar_one()
            if already:
                continue
            give = min(want, left)
            hold = WaitlistHold(
                id=uuid.uuid4(), entry_id=entry.id, event_id=event.id, user_id=entry.user_id,
                ticket_type_id=ticket_type.id, quantity=give, held_until=until,
                status=HOLD_ACTIVE,
            )
            db.add(hold)
            entry.status = WAITLIST_HELD
            created.append(hold.id)
            left -= give
    db.flush()
    if created:
        from src.jobs.dispatch import enqueue_after_commit

        enqueue_after_commit(db, "src.modules.events.waitlist_service.notify_holds",
                             ",".join(str(h) for h in created))
    note_stock(db, event)
    return created


def holds_for_buyer(db: Session, event_id: uuid.UUID, user_id: uuid.UUID) -> list[WaitlistHold]:
    return list(db.execute(
        select(WaitlistHold).where(
            WaitlistHold.event_id == event_id, WaitlistHold.user_id == user_id,
            WaitlistHold.status == HOLD_ACTIVE, WaitlistHold.held_until > _now(),
        ).with_for_update()
    ).scalars())


def attach_order(db: Session, order: TicketOrder) -> None:
    """The buyer started checkout: their holds for those types now ride on the order (whose
    own hold keeps the tickets from here)."""
    type_ids = set(db.execute(
        select(TicketOrderItem.ticket_type_id).where(TicketOrderItem.order_id == order.id)
    ).scalars())
    for hold in holds_for_buyer(db, order.event_id, order.buyer_id):
        if hold.ticket_type_id in type_ids:
            hold.status = HOLD_CHECKOUT
            hold.order_id = order.id
    db.flush()  # order_issued reads these back in the same transaction (no autoflush)


def order_issued(db: Session, order: TicketOrder) -> None:
    """The tickets were issued: holds on the order are used, and someone who got what they
    were holding for is off the line (spec status "bought")."""
    holds = list(db.execute(
        select(WaitlistHold).where(WaitlistHold.order_id == order.id,
                                   WaitlistHold.status == HOLD_CHECKOUT)
    ).scalars())
    if not holds:
        return
    for hold in holds:
        hold.status = HOLD_USED
    entry = db.get(WaitlistEntry, holds[0].entry_id)
    if entry is not None and entry.status in WAITLIST_IN_LINE:
        remaining = {k: v for k, v in (entry.wanted or {}).items()
                     if uuid.UUID(k) not in {h.ticket_type_id for h in holds}}
        entry.wanted = remaining
        entry.status = WAITLIST_WAITING if remaining else WAITLIST_BOUGHT


def order_released(db: Session, order: TicketOrder) -> set:
    """A checkout using holds expired or failed: the holds come back while they last, and
    lapse otherwise. Returns the ticket types freed for the next person."""
    freed = set()
    now = _now()
    for hold in db.execute(
        select(WaitlistHold).where(WaitlistHold.order_id == order.id,
                                   WaitlistHold.status == HOLD_CHECKOUT).with_for_update()
    ).scalars():
        hold.order_id = None
        if hold.held_until > now:
            hold.status = HOLD_ACTIVE
        else:
            hold.status = HOLD_EXPIRED
            _expire_entry(db, hold.entry_id)
            freed.add(hold.ticket_type_id)
    return freed


def _expire_entry(db: Session, entry_id: uuid.UUID) -> Optional[WaitlistEntry]:
    entry = db.get(WaitlistEntry, entry_id)
    if entry is not None and entry.status in WAITLIST_IN_LINE:
        entry.status = WAITLIST_EXPIRED
        return entry
    return None


def close_for_event(db: Session, event: Event) -> list[uuid.UUID]:
    """The event was cancelled (or started): everyone still in line leaves it. Returns who was
    in line, so a cancellation can tell them."""
    people = []
    for entry in db.execute(
        select(WaitlistEntry).where(WaitlistEntry.event_id == event.id,
                                    WaitlistEntry.status.in_(WAITLIST_IN_LINE)).with_for_update()
    ).scalars():
        _end_holds(db, entry, HOLD_CANCELLED)
        entry.status = WAITLIST_CLOSED
        people.append(entry.user_id)
    return people


# --- the sweep (events.expire_waitlist_offers, every minute) -----------------------------------

def expire() -> dict:
    """Holds that ran out pass on (and their holders leave the line); waitlists of events that
    have started close quietly."""
    from src.shared.config.database import SessionLocal

    db = SessionLocal()
    expired = closed = 0
    try:
        now = _now()
        due = db.execute(
            select(WaitlistHold.event_id).where(
                WaitlistHold.status == HOLD_ACTIVE, WaitlistHold.held_until <= now
            ).distinct()
        ).scalars().all()
        for event_id in due:
            event = db.get(Event, event_id)
            freed = set()
            told = []
            for hold in db.execute(
                select(WaitlistHold).where(
                    WaitlistHold.event_id == event_id, WaitlistHold.status == HOLD_ACTIVE,
                    WaitlistHold.held_until <= now,
                ).with_for_update(skip_locked=True)
            ).scalars():
                hold.status = HOLD_EXPIRED
                freed.add(hold.ticket_type_id)
                entry = _expire_entry(db, hold.entry_id)
                if entry is not None:
                    told.append(entry.user_id)
                expired += 1
            db.flush()
            if event is not None:
                allocate(db, event, freed)
            if told:
                from src.jobs.dispatch import enqueue_after_commit

                enqueue_after_commit(
                    db, "src.modules.events.waitlist_service.notify_hold_ended",
                    str(event_id), ",".join(str(u) for u in set(told)),
                )
            db.commit()

        started = db.execute(
            select(WaitlistEntry.event_id).join(Event, Event.id == WaitlistEntry.event_id).where(
                WaitlistEntry.status.in_(WAITLIST_IN_LINE), Event.starts_at <= now
            ).distinct()
        ).scalars().all()
        for event_id in started:
            closed += len(close_for_event(db, db.get(Event, event_id)))
            db.commit()
        return {"expired": expired, "closed": closed}
    finally:
        db.close()


# --- B26: telling savers when a sold-out event reopens -----------------------------------------

def note_stock(db: Session, event: Event) -> None:
    """Call after stock changes, inside the caller's transaction. Marks when the event sold
    out; when it reopens for the public (after waitlist holds), tells savers who aren't
    going, once per reopening."""
    if event.status != EVENT_PUBLISHED or event.ends_at <= _now():
        return
    sold_out = is_sold_out(db, event)
    if sold_out:
        if event.sold_out_at is None:
            event.sold_out_at = _now()
        return
    if event.sold_out_at is None:
        return
    reopened_after = _now() - event.sold_out_at
    event.sold_out_at = None
    if reopened_after < REOPEN_DEBOUNCE:
        return
    event.savers_notified_at = _now()
    from src.jobs.dispatch import enqueue_after_commit

    enqueue_after_commit(db, "src.modules.events.waitlist_service.notify_savers", str(event.id))


# --- notifications (background) ------------------------------------------------------------------

def _push(db, user_id, *, type_, event, title, body, payload=None) -> None:
    from src.modules.notifications import notifications_dao

    try:
        notifications_dao.create_and_push(
            db, user_id=user_id, type=type_, target_type="event", target_id=event.id,
            title=title, body=body, payload=payload or {},
        )
    except Exception:  # noqa: BLE001 — a notification never fails the work behind it
        logger.exception("Waitlist notification %s failed for %s", type_, user_id)


def _held_line(count: int, until: datetime, event: Event) -> str:
    from src.shared.templates.ticket_email import local_clock_label

    noun = "ticket is" if count == 1 else "tickets are"
    return f"{count} {noun} held for you until {local_clock_label(event, until)}."


def notify_holds(hold_ids_csv: str) -> None:
    """Spec: "2 tickets are held for you until 9:40 PM CST", by push and email."""
    from src.shared.config.database import SessionLocal
    from src.shared.notification.email_service import send_email
    from src.shared.templates.event_emails import simple_event_email

    ids = [uuid.UUID(h) for h in hold_ids_csv.split(",") if h]
    db = SessionLocal()
    try:
        holds = db.execute(select(WaitlistHold).where(WaitlistHold.id.in_(ids),
                                                      WaitlistHold.status == HOLD_ACTIVE)).scalars().all()
        by_user: dict = {}
        for hold in holds:
            by_user.setdefault(hold.user_id, []).append(hold)
        for user_id, mine in by_user.items():
            event = db.get(Event, mine[0].event_id)
            count = sum(h.quantity for h in mine)
            until = min(h.held_until for h in mine)
            line = _held_line(count, until, event)
            _push(db, user_id, type_="waitlist_hold", event=event,
                  title=f"Tickets for {event.title}", body=line,
                  payload={"heldUntil": until.isoformat()})
            user = db.get(User, user_id)
            if user is not None and user.email:
                send_email(user.email, f"Tickets held for you: {event.title}",
                           simple_event_email(event, "Your turn on the waitlist",
                                              [line, "Open Studio 3 to get them before the hold ends."]))
    finally:
        db.close()


def notify_hold_ended(event_id: str, user_ids_csv: str) -> None:
    from src.shared.config.database import SessionLocal

    db = SessionLocal()
    try:
        event = db.get(Event, uuid.UUID(event_id))
        if event is None:
            return
        for raw in user_ids_csv.split(","):
            if raw:
                _push(db, uuid.UUID(raw), type_="waitlist_hold_ended", event=event,
                      title=event.title,
                      body="The tickets held for you have gone to the next person in line.")
    finally:
        db.close()


def notify_savers(event_id: str) -> None:
    """B26 `event_tickets_available`: people who saved it and aren't going."""
    from src.shared.config.database import SessionLocal

    db = SessionLocal()
    try:
        event = db.get(Event, uuid.UUID(event_id))
        if event is None or event.status != EVENT_PUBLISHED:
            return
        holders = set(db.execute(
            select(Ticket.holder_id).where(Ticket.event_id == event.id,
                                           Ticket.status.in_(LIVE_TICKET_STATUSES))
        ).scalars())
        for user_id in db.execute(select(EventSave.user_id).where(EventSave.event_id == event.id)).scalars():
            if user_id in holders or user_id == event.host_id:
                continue
            _push(db, user_id, type_="event_tickets_available", event=event,
                  title=event.title, body="Tickets are available again.")
    finally:
        db.close()
