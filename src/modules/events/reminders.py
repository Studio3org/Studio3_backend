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
# The day-before reminder: once the event is under a day away, and more than 2 hours out (from
# there the 2-hour reminder takes over, so nobody gets both in one run).
AHEAD = timedelta(hours=24)
TOO_LATE = timedelta(hours=2)
# The ticketing spec's second reminder, "2 hours before" (hourly job, so it lands 1 to 2 hours
# before), skipped in the last 10 minutes.
SOON = timedelta(hours=2)
TOO_LATE_SOON = timedelta(minutes=10)


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


def due_events_query(now: datetime, *, soon: bool = False):
    low, high = (TOO_LATE_SOON, SOON) if soon else (TOO_LATE, AHEAD)
    return (
        select(Event)
        .where(
            Event.status == EVENT_PUBLISHED,
            Event.starts_at > now + low,
            Event.starts_at <= now + high,
        )
        .order_by(Event.starts_at)
    )


def claim(db: Session, event_id: uuid.UUID, now: datetime, *, soon: bool = False) -> list[uuid.UUID]:
    """Mark everyone going and not yet reminded, and return who they are. Committed, so a
    concurrent run claims nobody twice: the update takes each row once."""
    column = EventRsvp.reminded_soon_at if soon else EventRsvp.reminded_at
    claimed = list(
        db.execute(
            update(EventRsvp)
            .where(
                EventRsvp.event_id == event_id,
                EventRsvp.status == RSVP_GOING,
                column.is_(None),
            )
            .values({column.key: now})
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
            _email(event, user, message)
        except Exception:
            db.rollback()
            logger.exception("Event reminder failed for %s on event %s", user_id, event.id)
    return sent


def _email(event, user, message: str) -> None:
    """Spec: reminders to ticket holders also go by email."""
    if not getattr(user, "email", None):
        return
    try:
        from src.shared.notification.email_service import send_email
        from src.shared.templates.event_emails import simple_event_email

        send_email(user.email, f"Reminder: {event.title}",
                   simple_event_email(event, "Event reminder",
                                      [message, "Your tickets are in the Studio 3 app."]))
    except Exception:  # noqa: BLE001
        logger.exception("Reminder email failed for %s", user.id)


def send_event_reminders(db: Optional[Session] = None, now: Optional[datetime] = None) -> int:
    """The day before and 2 hours before, everyone going hears once each. Returns how many
    were sent."""
    own = db is None
    db = db or SessionLocal()
    try:
        now = now or datetime.now(timezone.utc)
        sent = 0
        for soon in (False, True):
            for event in list(db.execute(due_events_query(now, soon=soon)).scalars()):
                user_ids = claim(db, event.id, now, soon=soon)
                if user_ids:
                    sent += remind(db, event, user_ids, now)
        return sent
    finally:
        if own:
            db.close()
