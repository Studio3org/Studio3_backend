"""Event reminders (BACKEND_CHANGES B14): the day before, everyone going hears once.

An hourly job (`events.send_reminders`) picks published events starting between one and 24
hours from now and tells each person going who has not been reminded: "Night Market is
tomorrow at 7 PM at Cedars Union." The type is `event_reminder`, under Events in Settings.

Each RSVP is marked `reminded_at` and committed before anything is sent, so a second run, or
a redelivered job, finds nobody left (at worst a crash between the two loses a reminder,
never doubles one). An RSVP made after the job has passed still hears on the next run, unless
the event is under an hour away by then: a reminder that late is noise.

Going means an RSVP for now; with tickets (B22) it will mean a ticket.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.shared.config.database import SessionLocal
from src.shared.models.event import EVENT_PUBLISHED, RSVP_GOING, Event, EventRsvp
from src.shared.models.user import User
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

EVENT_REMINDER = "event_reminder"
# Reminded once the event is under a day away, and not in its last hour.
AHEAD = timedelta(hours=24)
TOO_LATE = timedelta(hours=1)


def _zone(name: Optional[str]) -> Optional[ZoneInfo]:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def _clock(moment: datetime) -> str:
    """7 PM, 7:30 PM."""
    hour = moment.hour % 12 or 12
    minutes = f":{moment.minute:02d}" if moment.minute else ""
    return f"{hour}{minutes} {'AM' if moment.hour < 12 else 'PM'}"


def reminder_message(event, now: datetime) -> str:
    """"Night Market is tomorrow at 7 PM at Cedars Union." In the event's own time zone; an
    event without one says how many hours away it is, since a bare time would be UTC."""
    venue = (event.venue_name or "").strip()
    where = f" at {venue}" if venue else ""
    zone = _zone(event.timezone)
    if zone is None:
        hours = max(1, round((event.starts_at - now).total_seconds() / 3600))
        return f"{event.title} starts in {hours} {'hour' if hours == 1 else 'hours'}{where}."
    start = event.starts_at.astimezone(zone)
    days = (start.date() - now.astimezone(zone).date()).days
    day = "today" if days <= 0 else "tomorrow" if days == 1 else start.strftime("%A")
    return f"{event.title} is {day} at {_clock(start)}{where}."


def due_events_query(now: datetime):
    return (
        select(Event)
        .where(
            Event.status == EVENT_PUBLISHED,
            Event.starts_at > now + TOO_LATE,
            Event.starts_at <= now + AHEAD,
        )
        .order_by(Event.starts_at)
    )


def claim(db: Session, event_id: uuid.UUID, now: datetime) -> list[uuid.UUID]:
    """Mark everyone going and not yet reminded, and return who they are. Committed, so a
    concurrent run claims nobody twice: the update takes each row once."""
    claimed = list(
        db.execute(
            update(EventRsvp)
            .where(
                EventRsvp.event_id == event_id,
                EventRsvp.status == RSVP_GOING,
                EventRsvp.reminded_at.is_(None),
            )
            .values(reminded_at=now)
            .returning(EventRsvp.user_id)
        ).scalars()
    )
    db.commit()
    return claimed


def remind(db: Session, event, user_ids: list[uuid.UUID], now: datetime) -> int:
    """Send to each person; one failing does not stop the rest. Returns how many were sent."""
    from src.modules.notifications import notifications_dao

    message = reminder_message(event, now)
    sent = 0
    for user_id in user_ids:
        user = db.get(User, user_id)
        if user is None or user.deleted_at is not None:
            continue
        try:
            notifications_dao.create_and_push(
                db,
                user_id=user_id,
                type=EVENT_REMINDER,
                actor_id=event.host_id,
                target_type="event",
                target_id=event.id,
                payload={"eventTitle": event.title, "startsAt": event.starts_at.isoformat()},
                title="Event reminder",
                body=message,
            )
            sent += 1
        except Exception:
            db.rollback()
            logger.exception("Event reminder failed for %s on event %s", user_id, event.id)
    return sent


def send_event_reminders(db: Optional[Session] = None, now: Optional[datetime] = None) -> int:
    """Remind everyone going to an event in the next day. Returns how many were sent."""
    own = db is None
    db = db or SessionLocal()
    try:
        now = now or datetime.now(timezone.utc)
        sent = 0
        for event in list(db.execute(due_events_query(now)).scalars()):
            user_ids = claim(db, event.id, now)
            if user_ids:
                sent += remind(db, event, user_ids, now)
        return sent
    finally:
        if own:
            db.close()
