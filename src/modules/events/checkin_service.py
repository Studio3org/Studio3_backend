"""Check-in at the door (ticketing spec, "Check-in at the door"; BACKEND_CHANGES B28).

* Only the host and co-hosts can scan, and each ticket checks in once.
* A scan names the ticket by its QR value (signed, so a made-up one fails), its number for a
  QR that won't scan ("Enter code"), or its id (the guest list's "check in by hand").
* Results: `checked_in` (name, type, "1 of 2"), `already_checked_in` (with when), or
  `invalid` (wrong event, refunded, cancelled, or not a ticket at all).
* **Offline.** Each device keeps its scans with the time they happened and syncs later. The
  earliest scan of a ticket wins: if a device syncs a scan from before the one on record, the
  record moves to it and the later device reads "Already checked in". Signed codes let a
  device tell a real ticket from a fake without the network (`manifest` hands out the public
  key and the guest list).

Each ticket row is locked while it is checked, so two doors scanning one code at the same
moment get one `checked_in` and one `already_checked_in`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.shared.models.event import (
    EVENT_ARCHIVED,
    EVENT_PUBLISHED,
    ROLE_COHOST,
    Event,
)
from src.shared.models.ticketing import (
    LIVE_TICKET_STATUSES,
    RSVP_TICKET_NAME,
    TICKET_CHECKED_IN,
    TICKET_VALID,
    Ticket,
    TicketType,
)
from src.shared.models.user import User
from src.shared.ticketing import ticket_codes
from src.shared.utils.app_error import AppError

RESULT_CHECKED_IN = "checked_in"
RESULT_ALREADY = "already_checked_in"
RESULT_INVALID = "invalid"

MAX_SYNC = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def can_scan(db: Session, event: Event, user_id: Optional[uuid.UUID]) -> bool:
    """The host and co-hosts (spec). Phase 5 narrows co-hosts to those who accepted."""
    if user_id is None:
        return False
    if user_id == event.host_id:
        return True
    from src.modules.events import people_service

    return people_service.is_accepted(db, event.id, user_id, ROLE_COHOST)


def require_scanner(db: Session, event: Event, user_id) -> None:
    if not can_scan(db, event, user_id):
        raise AppError("Event not found.", 404)
    if event.status not in (EVENT_PUBLISHED, EVENT_ARCHIVED):
        raise AppError("This event isn't checking anyone in.", 409)


# --- finding the ticket -------------------------------------------------------------------------

def _find(db: Session, event: Event, code: str) -> tuple[Optional[Ticket], Optional[str]]:
    """(ticket, reason it's no good)."""
    code = (code or "").strip()
    if not code:
        return None, "Not a valid ticket."
    if code.startswith(ticket_codes.PREFIX):
        parsed = ticket_codes.read_code(code)
        if parsed is None:
            return None, "Not a valid ticket."
        ticket_id, event_id = parsed
        if event_id != event.id:
            return None, "Not a valid ticket: it's for a different event."
        stmt = select(Ticket).where(Ticket.id == ticket_id)
    else:
        try:
            stmt = select(Ticket).where(Ticket.id == uuid.UUID(code))
        except ValueError:
            stmt = select(Ticket).where(func.upper(Ticket.number) == code.upper())
    ticket = db.execute(stmt.with_for_update()).scalar_one_or_none()
    if ticket is None:
        return None, "Not a valid ticket."
    if ticket.event_id != event.id:
        return None, "Not a valid ticket: it's for a different event."
    return ticket, None


def _position(db: Session, ticket: Ticket) -> tuple[int, int]:
    """Which of the holder's tickets in this order it is ("1 of 2")."""
    ids = db.execute(
        select(Ticket.id).where(Ticket.order_id == ticket.order_id)
        .order_by(Ticket.created_at, Ticket.number)
    ).scalars().all()
    return (ids.index(ticket.id) + 1 if ticket.id in ids else 1), len(ids)


def _ticket_view(db: Session, ticket: Ticket) -> dict:
    holder = db.get(User, ticket.holder_id)
    type_name = None
    if ticket.ticket_type_id:
        type_name = db.execute(select(TicketType.name).where(TicketType.id == ticket.ticket_type_id)).scalar()
    index, total = _position(db, ticket)
    return {
        "id": str(ticket.id),
        "number": ticket.number,
        "ticketTypeName": type_name or RSVP_TICKET_NAME,
        "holderName": holder.name if holder else None,
        "holderUsername": holder.username if holder else None,
        "holderAvatarUrl": holder.image if holder else None,
        "position": index,
        "of": total,
        "status": ticket.status,
        "checkedInAt": ticket.checked_in_at.isoformat() if ticket.checked_in_at else None,
    }


def _clock(event: Event, moment: datetime) -> str:
    from src.shared.templates.ticket_email import local_clock_label

    return local_clock_label(event, moment)


# --- checking in -----------------------------------------------------------------------------

def _scan_time(raw) -> Optional[datetime]:
    if raw in (None, ""):
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        raise AppError("scannedAt must be an ISO 8601 date-time.", 400) from None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    # A device clock can drift; a scan can't be from the future.
    return min(value, _now())


def check_in(db: Session, event: Event, actor_id: uuid.UUID, code: str,
             scanned_at: Optional[datetime] = None) -> dict:
    """One scan. Caller commits. Never raises for a bad ticket: the result says so."""
    ticket, problem = _find(db, event, code)
    if ticket is None:
        return {"result": RESULT_INVALID, "message": problem, "ticket": None}
    when = scanned_at or _now()
    if ticket.status == TICKET_VALID:
        ticket.status = TICKET_CHECKED_IN
        ticket.checked_in_at = when
        ticket.checked_in_by_id = actor_id
        view = _ticket_view(db, ticket)
        return {"result": RESULT_CHECKED_IN, "message": f"Checked in: {view['holderName'] or 'Guest'}, "
                f"{view['ticketTypeName']} ({view['position']} of {view['of']}).", "ticket": view}
    if ticket.status == TICKET_CHECKED_IN:
        if scanned_at is not None and ticket.checked_in_at and scanned_at < ticket.checked_in_at:
            # Offline: this device scanned it first. The earliest scan wins.
            ticket.checked_in_at = scanned_at
            ticket.checked_in_by_id = actor_id
            view = _ticket_view(db, ticket)
            return {"result": RESULT_CHECKED_IN, "message": "Checked in.", "ticket": view}
        view = _ticket_view(db, ticket)
        return {"result": RESULT_ALREADY,
                "message": f"Already checked in at {_clock(event, ticket.checked_in_at)}.",
                "ticket": view}
    return {"result": RESULT_INVALID, "message": "Not a valid ticket: it was refunded or cancelled.",
            "ticket": _ticket_view(db, ticket)}


def sync(db: Session, event: Event, actor_id: uuid.UUID, scans) -> list[dict]:
    """Offline scans, applied oldest first. Caller commits."""
    if not isinstance(scans, list):
        raise AppError("scans must be a list.", 400)
    if len(scans) > MAX_SYNC:
        raise AppError(f"Sync at most {MAX_SYNC} scans at a time.", 400)
    parsed = []
    for index, scan in enumerate(scans):
        if not isinstance(scan, dict):
            raise AppError("Each scan needs a code and a scannedAt.", 400)
        parsed.append((_scan_time(scan.get("scannedAt")) or _now(), index, str(scan.get("code") or "")))
    results: list = [None] * len(parsed)
    for when, index, code in sorted(parsed):
        result = check_in(db, event, actor_id, code, scanned_at=when)
        result["code"] = code
        results[index] = result
    return results


# --- the guest list ------------------------------------------------------------------------------

def counts(db: Session, event_id: uuid.UUID) -> dict:
    rows = dict(db.execute(
        select(Ticket.status, func.count(Ticket.id)).where(
            Ticket.event_id == event_id, Ticket.status.in_(LIVE_TICKET_STATUSES)
        ).group_by(Ticket.status)
    ).all())
    checked = int(rows.get(TICKET_CHECKED_IN, 0))
    return {"checkedIn": checked, "total": checked + int(rows.get(TICKET_VALID, 0))}


def guests(db: Session, event: Event, query: str = "", limit: int = 200) -> dict:
    """Admissions that still count, searchable by name, username or ticket number."""
    stmt = (
        select(Ticket).join(User, User.id == Ticket.holder_id)
        .where(Ticket.event_id == event.id, Ticket.status.in_(LIVE_TICKET_STATUSES))
    )
    q = (query or "").strip()
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        stmt = stmt.where(or_(User.name.ilike(like), User.username.ilike(like),
                              Ticket.number.ilike(like)))
    tickets = db.execute(stmt.order_by(User.name, Ticket.number).limit(max(1, min(limit, 1000)))).scalars()
    return {
        "guests": [_ticket_view(db, t) for t in tickets],
        **counts(db, event.id),
        "ageLimit": event.age_limit,
    }


def manifest(db: Session, event: Event) -> dict:
    """What a scanner needs to work offline: the key that checks a code's signature, and
    every admission with its state as of now."""
    tickets = db.execute(
        select(Ticket).where(Ticket.event_id == event.id,
                             Ticket.status.in_(LIVE_TICKET_STATUSES))
        .order_by(Ticket.created_at)
    ).scalars().all()
    return {
        "eventId": str(event.id),
        "publicKey": ticket_codes.public_key_b64(),
        "codePrefix": ticket_codes.PREFIX,
        "ageLimit": event.age_limit,
        "generatedAt": _now().isoformat(),
        "tickets": [_ticket_view(db, t) for t in tickets],
        **counts(db, event.id),
    }

