"""Reading and writing events.

The listing queries all filter on `published`: a draft is a private working copy and must
never surface in a feed, a category or a search. That is enforced here rather than at each
call site, because a single forgotten filter leaks an unfinished event to everybody.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.shared.models.event import (
    EVENT_CATEGORIES,
    EVENT_PUBLISHED,
    ROLE_ARTIST,
    ROLE_COHOST,
    Event,
    EventParticipant,
    EventPiece,
    EventSave,
)
from src.shared.models.social import Follow
from src.shared.models.user import User
from src.shared.utils.app_error import AppError

MAX_TITLE = 140
DEFAULT_LIMIT = 20
MAX_LIMIT = 50


def get_event(db: Session, event_id: uuid.UUID) -> Optional[Event]:
    return db.get(Event, event_id)


def get_event_for_update(db: Session, event_id: uuid.UUID) -> Optional[Event]:
    return db.execute(
        select(Event).where(Event.id == event_id).with_for_update()
    ).scalar_one_or_none()


def create_event(
    db: Session,
    host: User,
    *,
    title: str,
    starts_at: datetime,
    ends_at: datetime,
    description: Optional[str] = None,
    cover_media_url: Optional[str] = None,
    category: Optional[str] = None,
    tz: Optional[str] = None,
    venue_name: Optional[str] = None,
    address: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    capacity: Optional[int] = None,
    commit: bool = True,
) -> Event:
    """Create an event in draft. Publishing is a separate, deliberate step.

    Draft is the default because publishing is what puts work on sale — a host needs to be
    able to build and rearrange a bill without anything going live under them.
    """
    validate_window(starts_at, ends_at)
    event = Event(
        id=uuid.uuid4(),
        host_id=host.id,
        title=validate_title(title),
        description=(description or "").strip() or None,
        cover_media_url=cover_media_url,
        category=validate_category(category),
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=tz,
        venue_name=(venue_name or "").strip() or None,
        address=(address or "").strip() or None,
        latitude=latitude,
        longitude=longitude,
        capacity=_clean_capacity(capacity),
    )
    db.add(event)
    if commit:
        db.commit()
        db.refresh(event)
    else:
        db.flush()
    return event


def validate_title(title: Optional[str]) -> str:
    cleaned = (title or "").strip()
    if not cleaned:
        raise AppError("An event needs a title.", 400)
    if len(cleaned) > MAX_TITLE:
        raise AppError(f"An event title can be at most {MAX_TITLE} characters.", 400)
    return cleaned


def validate_category(category: Optional[str]) -> Optional[str]:
    if category is None or category == "":
        return None
    if category not in EVENT_CATEGORIES:
        raise AppError(f"Unknown category '{category}'.", 400)
    return category


def _clean_capacity(capacity) -> Optional[int]:
    """None means unlimited, which is the default and the common case."""
    if capacity is None or capacity == "":
        return None
    try:
        value = int(capacity)
    except (TypeError, ValueError):
        raise AppError("Capacity must be a whole number.", 400) from None
    if value <= 0:
        raise AppError("Capacity must be at least 1, or left empty for unlimited.", 400)
    return value


def validate_capacity(db: Session, event: Event, capacity) -> Optional[int]:
    """A new cap for an event that may already have people coming.

    Lowering it below the current headcount is refused rather than silently applied: the
    people already on the list said yes in good faith, and there is no mechanism — and no
    product decision — for choosing which of them to turn away.
    """
    value = _clean_capacity(capacity)
    if value is None:
        return None
    from src.modules.events import rsvp_service

    going = rsvp_service.going_count(db, event.id)
    if value < going:
        raise AppError(
            f"{going} people have already said they're coming, so capacity can't be set "
            f"below {going}.",
            409,
        )
    return value


def validate_window(starts_at: datetime, ends_at: datetime) -> None:
    """When the event runs.

    An end before a start is refused here as well as by a CHECK constraint, because the
    error a host sees should say what is wrong rather than surfacing a constraint name — and
    because an event auction's close is computed backwards from the end time, so an inverted
    window would produce an auction that closes before it opens.
    """
    if starts_at is None or ends_at is None:
        raise AppError("An event needs a start and an end time.", 400)
    if ends_at <= starts_at:
        raise AppError("An event has to end after it starts.", 400)


# --- participants ---------------------------------------------------------------------------

def set_participants(
    db: Session,
    event: Event,
    *,
    role: str,
    user_ids: list[uuid.UUID],
    commit: bool = True,
) -> list[EventParticipant]:
    """Replace the whole set for one role.

    Replace rather than append: the create flow shows one editable list per role, so a
    partial update would make removing somebody impossible from the only UI that exists.
    """
    if role not in (ROLE_COHOST, ROLE_ARTIST):
        raise AppError(f"Unknown participant role '{role}'.", 400)

    for existing in db.execute(
        select(EventParticipant).where(
            EventParticipant.event_id == event.id, EventParticipant.role == role
        )
    ).scalars():
        db.delete(existing)
    db.flush()

    rows = []
    seen: set[uuid.UUID] = set()
    for index, user_id in enumerate(user_ids):
        if user_id in seen or user_id == event.host_id:
            # The host is already the host; listing them again as a cohost reads as a bug.
            continue
        seen.add(user_id)
        row = EventParticipant(
            id=uuid.uuid4(), event_id=event.id, user_id=user_id, role=role, sort_order=index
        )
        db.add(row)
        rows.append(row)
    if commit:
        db.commit()
    else:
        db.flush()
    return rows


def list_participants(db: Session, event_id: uuid.UUID, role: Optional[str] = None) -> list:
    stmt = select(EventParticipant, User).join(User, User.id == EventParticipant.user_id).where(
        EventParticipant.event_id == event_id
    )
    if role:
        stmt = stmt.where(EventParticipant.role == role)
    return list(db.execute(stmt.order_by(EventParticipant.sort_order.asc())).all())


# --- saves ----------------------------------------------------------------------------------

def set_saved(db: Session, event_id: uuid.UUID, user_id: uuid.UUID, saved: bool) -> bool:
    existing = db.execute(
        select(EventSave).where(EventSave.event_id == event_id, EventSave.user_id == user_id)
    ).scalar_one_or_none()
    if saved and existing is None:
        db.add(EventSave(id=uuid.uuid4(), event_id=event_id, user_id=user_id))
    elif not saved and existing is not None:
        db.delete(existing)
    db.commit()
    return saved


def is_saved(db: Session, event_id: uuid.UUID, user_id: Optional[uuid.UUID]) -> bool:
    if user_id is None:
        return False
    return db.execute(
        select(func.count(EventSave.id)).where(
            EventSave.event_id == event_id, EventSave.user_id == user_id
        )
    ).scalar_one() > 0


def count_saves(db: Session, event_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count(EventSave.id)).where(EventSave.event_id == event_id)
    ).scalar_one()


# --- listings ---------------------------------------------------------------------------------

def _published_upcoming(now: datetime):
    """The base every public listing narrows.

    Keyed on `ends_at`, not `starts_at`: an event that started an hour ago and runs until
    midnight is still happening, and dropping it from "upcoming" the moment it began is how
    a listing loses the events someone is most likely to be looking for right now.
    """
    return select(Event).where(Event.status == EVENT_PUBLISHED, Event.ends_at > now)


def list_upcoming(
    db: Session,
    *,
    category: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> list[Event]:
    stmt = _published_upcoming(datetime.now(timezone.utc))
    if category:
        stmt = stmt.where(Event.category == validate_category(category))
    return list(
        db.execute(
            stmt.order_by(Event.starts_at.asc()).limit(_clamp(limit)).offset(max(offset, 0))
        ).scalars()
    )


def list_today(db: Session, *, limit: int = DEFAULT_LIMIT) -> list[Event]:
    """Happening within the next 24 hours, including anything already under way."""
    now = datetime.now(timezone.utc)
    return list(
        db.execute(
            _published_upcoming(now)
            .where(Event.starts_at <= now + timedelta(hours=24))
            .order_by(Event.starts_at.asc())
            .limit(_clamp(limit))
        ).scalars()
    )


def list_from_following(
    db: Session, viewer_id: uuid.UUID, *, limit: int = DEFAULT_LIMIT
) -> list[Event]:
    """Events hosted by, or featuring, someone the viewer follows.

    Participants count, not just hosts: an artist showing at a gallery's event is the reason
    most people care about it, and keying only on the host would hide exactly those.
    """
    now = datetime.now(timezone.utc)
    # Accepted only. A pending request to a private account is not a follow yet, and
    # surfacing that account's events would leak which events a private host is running.
    followed = select(Follow.following_id).where(
        Follow.follower_id == viewer_id, Follow.status == "accepted"
    )
    participating = select(EventParticipant.event_id).where(
        EventParticipant.user_id.in_(followed)
    )
    return list(
        db.execute(
            _published_upcoming(now)
            .where(or_(Event.host_id.in_(followed), Event.id.in_(participating)))
            .order_by(Event.starts_at.asc())
            .limit(_clamp(limit))
        ).scalars()
    )


def list_saved(db: Session, viewer_id: uuid.UUID, *, limit: int = DEFAULT_LIMIT) -> list[Event]:
    saved = select(EventSave.event_id).where(EventSave.user_id == viewer_id)
    return list(
        db.execute(
            select(Event)
            .where(Event.status == EVENT_PUBLISHED, Event.id.in_(saved))
            .order_by(Event.starts_at.asc())
            .limit(_clamp(limit))
        ).scalars()
    )


def list_for_host(
    db: Session, host_id: uuid.UUID, *, viewer_id: Optional[uuid.UUID] = None
) -> list[Event]:
    """A host's own events. Drafts are included only for the host themselves."""
    stmt = select(Event).where(Event.host_id == host_id)
    if viewer_id != host_id:
        stmt = stmt.where(Event.status == EVENT_PUBLISHED)
    return list(db.execute(stmt.order_by(Event.starts_at.desc())).scalars())


def category_summaries(db: Session) -> list[dict]:
    """Upcoming count and a cover image per category, for the browse tiles.

    The cover is borrowed from the soonest upcoming event in that category rather than being
    a fixed asset. A category with nothing in it has no tile at all, so there is never an
    image standing in for an empty section — and the artwork on the tile is always something
    actually happening.
    """
    now = datetime.now(timezone.utc)
    rows = db.execute(
        select(Event.category, Event.cover_media_url, Event.starts_at)
        .where(Event.status == EVENT_PUBLISHED, Event.ends_at > now, Event.category.is_not(None))
        .order_by(Event.category.asc(), Event.starts_at.asc())
    ).all()

    summaries: dict[str, dict] = {}
    for category, cover, _starts_at in rows:
        entry = summaries.setdefault(
            category, {"id": category, "upcomingCount": 0, "coverMediaUrl": None}
        )
        entry["upcomingCount"] += 1
        # Rows arrive soonest-first within a category, so the first cover seen is the one.
        if entry["coverMediaUrl"] is None and cover:
            entry["coverMediaUrl"] = cover
    return [summaries[key] for key in sorted(summaries)]


def count_lineup(db: Session, event_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count(EventPiece.id)).where(EventPiece.event_id == event_id)
    ).scalar_one()


def _clamp(limit: int) -> int:
    return max(1, min(limit or DEFAULT_LIMIT, MAX_LIMIT))
