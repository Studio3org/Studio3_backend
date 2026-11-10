"""Who is on an event's bill, and what they may do (ticketing spec, "Lineup"; BACKEND_CHANGES
B28).

* **Invites.** Adding a co-host or featured artist invites them (notification and email).
  They show on the event, and a co-host can act on it, only once they accept. Declining is
  final for that invite; the host can invite them again.
* **Co-hosts** who accepted can edit the event, manage tickets and the lineup, and scan at the
  door. Publishing, cancelling, rescheduling, deleting and changing who is on the bill stay
  the host's.

A row here is a credit and, for an accepted co-host, a permission. It never lets anyone sell
another artist's work: that takes the artist's own approval (lineup_service).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.shared.models.event import (
    EVENT_CANCELLED,
    EVENT_DRAFT,
    INVITE_ACCEPTED,
    INVITE_DECLINED,
    INVITE_INVITED,
    PARTICIPANT_ROLES,
    ROLE_ARTIST,
    ROLE_COHOST,
    Event,
    EventParticipant,
)
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row(db: Session, event_id, user_id, role) -> Optional[EventParticipant]:
    return db.execute(
        select(EventParticipant).where(
            EventParticipant.event_id == event_id, EventParticipant.user_id == user_id,
            EventParticipant.role == role,
        )
    ).scalars().first()


def is_accepted(db: Session, event_id: uuid.UUID, user_id: uuid.UUID, role: str) -> bool:
    row = _row(db, event_id, user_id, role)
    return row is not None and row.status == INVITE_ACCEPTED


def can_edit(db: Session, event: Event, user_id: Optional[uuid.UUID]) -> bool:
    """The host and accepted co-hosts (spec: "Co-hosts: can edit the event and scan tickets")."""
    if user_id is None:
        return False
    return user_id == event.host_id or is_accepted(db, event.id, user_id, ROLE_COHOST)


def is_on_bill(db: Session, event: Event, user_id: Optional[uuid.UUID]) -> bool:
    """Invited or accepted (not declined): may see the event while it's still a draft."""
    if user_id is None:
        return False
    return db.execute(
        select(EventParticipant.id).where(
            EventParticipant.event_id == event.id, EventParticipant.user_id == user_id,
            EventParticipant.status != INVITE_DECLINED,
        ).limit(1)
    ).first() is not None


# --- the host setting the bill -------------------------------------------------------------------

def set_people(db: Session, event: Event, *, role: str, user_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Make `user_ids` the list for `role`. Keeps everyone already on it as they are, invites
    the new ones, removes the rest. Returns who was newly invited (told after the commit)."""
    if role not in PARTICIPANT_ROLES:
        raise AppError(f"Unknown participant role '{role}'.", 400)
    current = {
        row.user_id: row for row in db.execute(
            select(EventParticipant).where(EventParticipant.event_id == event.id,
                                           EventParticipant.role == role)
        ).scalars()
    }
    wanted: list[uuid.UUID] = []
    for user_id in user_ids:
        if user_id != event.host_id and user_id not in wanted:
            wanted.append(user_id)
    for user_id, row in current.items():
        if user_id not in wanted:
            db.delete(row)
    invited = []
    for index, user_id in enumerate(wanted):
        row = current.get(user_id)
        if row is None:
            db.add(EventParticipant(id=uuid.uuid4(), event_id=event.id, user_id=user_id,
                                    role=role, sort_order=index, status=INVITE_INVITED,
                                    invited_at=_now()))
            invited.append(user_id)
        else:
            row.sort_order = index
            if row.status == INVITE_DECLINED:
                # Putting someone back on the list asks them again.
                row.status = INVITE_INVITED
                row.invited_at = _now()
                row.responded_at = None
                invited.append(user_id)
    db.flush()
    return invited


def notify_invited(db: Session, event: Event, role: str, user_ids: list[uuid.UUID]) -> None:
    """Spec: "Invited as co-host or featured artist — that person — Email" (and a push)."""
    if not user_ids or event.status == EVENT_CANCELLED:
        return
    from src.modules.notifications import notifications_dao
    from src.shared.notification.email_service import send_email
    from src.shared.templates.event_emails import simple_event_email

    host = db.get(User, event.host_id)
    who = (host.name or f"@{host.username}") if host else "A host"
    what = "co-host" if role == ROLE_COHOST else "featured artist"
    line = f"{who} invited you to be a {what} at {event.title}."
    for user_id in user_ids:
        try:
            notifications_dao.create_and_push(
                db, user_id=user_id, type="event_invite", actor_id=event.host_id,
                target_type="event", target_id=event.id, title="Event invite", body=line,
                payload={"role": role, "eventTitle": event.title},
            )
        except Exception:  # noqa: BLE001
            logger.exception("Invite notification failed for %s", user_id)
        user = db.get(User, user_id)
        if user is not None and user.email:
            send_email(user.email, f"You're invited: {event.title}",
                       simple_event_email(event, "Event invite",
                                          [line, "Open Studio 3 to accept or decline."]))


# --- the invitee ---------------------------------------------------------------------------------

def respond(db: Session, event: Event, user: User, *, accept: bool, role: Optional[str] = None) -> dict:
    """Accept or decline this event's invites (both roles unless `role` is given)."""
    stmt = select(EventParticipant).where(EventParticipant.event_id == event.id,
                                          EventParticipant.user_id == user.id,
                                          EventParticipant.status == INVITE_INVITED)
    if role:
        if role not in PARTICIPANT_ROLES:
            raise AppError(f"Unknown participant role '{role}'.", 400)
        stmt = stmt.where(EventParticipant.role == role)
    rows = list(db.execute(stmt.with_for_update()).scalars())
    if not rows:
        raise AppError("There's no invite waiting for you on this event.", 404)
    status = INVITE_ACCEPTED if accept else INVITE_DECLINED
    for row in rows:
        row.status = status
        row.responded_at = _now()
    db.commit()
    _tell_host(db, event, user, accept, [r.role for r in rows])
    return {"status": status, "roles": sorted({r.role for r in rows})}


def _tell_host(db: Session, event: Event, user: User, accepted: bool, roles: list[str]) -> None:
    from src.modules.notifications import notifications_dao

    what = "co-host" if ROLE_COHOST in roles else "featured artist"
    verb = "accepted" if accepted else "declined"
    try:
        notifications_dao.create_and_push(
            db, user_id=event.host_id, type="event_invite_response", actor_id=user.id,
            target_type="event", target_id=event.id, title=event.title,
            body=f"{user.name or '@' + user.username} {verb} your invite to be a {what}.",
            payload={"accepted": accepted, "roles": roles},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Invite response notification failed for event %s", event.id)


def pending_invites(db: Session, user_id: uuid.UUID) -> list[tuple[EventParticipant, Event]]:
    return list(db.execute(
        select(EventParticipant, Event).join(Event, Event.id == EventParticipant.event_id).where(
            EventParticipant.user_id == user_id, EventParticipant.status == INVITE_INVITED,
            Event.status.in_((EVENT_DRAFT, "published")),
        ).order_by(Event.starts_at)
    ).all())


# --- what the event shows ------------------------------------------------------------------------

def people_view(db: Session, event: Event, viewer_id: Optional[uuid.UUID]) -> dict:
    """`cohosts` and `artists` (accepted only, everyone sees), and for those who can edit,
    `invites` with everyone's status (spec: "the host sees Invited / Accepted")."""
    rows = db.execute(
        select(EventParticipant, User).join(User, User.id == EventParticipant.user_id)
        .where(EventParticipant.event_id == event.id).order_by(EventParticipant.sort_order)
    ).all()

    def person(user: User) -> dict:
        return {"username": user.username, "name": user.name, "avatarUrl": user.image}

    data = {
        "cohosts": [person(u) for r, u in rows if r.role == ROLE_COHOST and r.status == INVITE_ACCEPTED],
        "artists": [person(u) for r, u in rows if r.role == ROLE_ARTIST and r.status == INVITE_ACCEPTED],
    }
    if can_edit(db, event, viewer_id):
        data["invites"] = [{**person(u), "role": r.role, "status": r.status} for r, u in rows]
    if viewer_id is not None:
        mine = [r for r, u in rows if r.user_id == viewer_id]
        if mine:
            data["viewerInvite"] = {"roles": sorted({r.role for r in mine}),
                                    "status": mine[0].status}
        data["viewerCanEdit"] = can_edit(db, event, viewer_id)
    return data
