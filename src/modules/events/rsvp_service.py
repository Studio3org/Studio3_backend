"""Saying you'll be there, and being told when that changes.

An RSVP is not a ticket and this file never charges anyone. Entry is free and open — the
value of an RSVP is that the host knows how many people to expect, and that we have somebody
to tell when the event moves or is called off.

That second half is the part worth building carefully. Before this, cancelling an event took
down its listings and told the *bidders*, and said nothing at all to the people who had
arranged their evening around turning up.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.shared.models.event import (
    RSVP_CANCELLED,
    RSVP_GOING,
    Event,
    EventRsvp,
)
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)


def going_count(db: Session, event_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count(EventRsvp.id)).where(
            EventRsvp.event_id == event_id, EventRsvp.status == RSVP_GOING
        )
    ).scalar_one()


def spots_left(db: Session, event: Event) -> Optional[int]:
    """How many places remain, or None when the event is uncapped.

    None is not zero and the two must not be conflated: an uncapped event has no number to
    show, while a full one has exactly zero.
    """
    if event.capacity is None:
        return None
    return max(0, event.capacity - going_count(db, event.id))


def is_full(db: Session, event: Event) -> bool:
    remaining = spots_left(db, event)
    return remaining is not None and remaining <= 0


def viewer_rsvp(
    db: Session, event_id: uuid.UUID, user_id: Optional[uuid.UUID]
) -> Optional[EventRsvp]:
    if user_id is None:
        return None
    return db.execute(
        select(EventRsvp).where(
            EventRsvp.event_id == event_id, EventRsvp.user_id == user_id
        )
    ).scalar_one_or_none()


def set_rsvp(db: Session, event: Event, user: User, *, going: bool) -> EventRsvp:
    """Say you are coming, or take it back.

    The capacity check is done under a row lock on the event, so two people claiming the last
    place serialise rather than both being told yes. Without it a capped event quietly
    oversells — which for a room with a fire limit is somebody standing outside.
    """
    if event.status != "published":
        raise AppError("This event isn't open for RSVPs.", 409)
    if going and event.ends_at <= datetime.now(timezone.utc):
        raise AppError("This event has already ended.", 409)
    if going and event.host_id == user.id:
        raise AppError("You're hosting this one.", 400)

    existing = viewer_rsvp(db, event.id, user.id)

    if not going:
        if existing is None or existing.status == RSVP_CANCELLED:
            # Idempotent: cancelling an RSVP you do not have is not an error.
            return existing or EventRsvp(
                id=uuid.uuid4(), event_id=event.id, user_id=user.id, status=RSVP_CANCELLED
            )
        existing.status = RSVP_CANCELLED
        existing.cancelled_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(existing)
        return existing

    if existing is not None and existing.status == RSVP_GOING:
        return existing

    # Lock the event before counting, so the count cannot change under us between the check
    # and the insert.
    locked = db.execute(
        select(Event).where(Event.id == event.id).with_for_update()
    ).scalar_one_or_none()
    if locked is None:
        raise AppError("Event not found.", 404)
    if is_full(db, locked):
        raise AppError(
            "This event is full. The host may open more places closer to the date.", 409
        )

    if existing is not None:
        existing.status = RSVP_GOING
        existing.cancelled_at = None
        db.commit()
        db.refresh(existing)
        return existing

    rsvp = EventRsvp(id=uuid.uuid4(), event_id=event.id, user_id=user.id, status=RSVP_GOING)
    db.add(rsvp)
    db.commit()
    db.refresh(rsvp)
    return rsvp


def list_attendees(db: Session, event_id: uuid.UUID) -> list[User]:
    """Who is coming. Host-only — an attendee list is not public."""
    return list(
        db.execute(
            select(User)
            .join(EventRsvp, EventRsvp.user_id == User.id)
            .where(EventRsvp.event_id == event_id, EventRsvp.status == RSVP_GOING)
            .order_by(EventRsvp.created_at.asc())
        ).scalars()
    )


def notify_attendees(db: Session, event: Event, *, type: str, title: str, body: str) -> int:
    """Tell everyone who said they were coming. Returns how many were reached.

    Call after the caller's commit. Failures are logged rather than raised: an event that has
    been cancelled stays cancelled even if a push fails, and rolling that back because one
    device was unreachable would be worse than a missed notification.
    """
    from src.modules.notifications import notifications_dao

    sent = 0
    for attendee in list_attendees(db, event.id):
        try:
            notifications_dao.create_and_push(
                db,
                user_id=attendee.id,
                type=type,
                target_type="event",
                target_id=event.id,
                title=title,
                body=body,
            )
            sent += 1
        except Exception:
            logger.exception(
                "Attendee notification failed for %s on event %s", attendee.id, event.id
            )
    return sent
