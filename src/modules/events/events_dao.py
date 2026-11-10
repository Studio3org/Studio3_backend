"""Reading and writing events.

The listing queries all filter on `published`: a draft is a private working copy and must
never surface in a feed, a category or a search. That is enforced here rather than at each
call site, because a single forgotten filter leaks an unfinished event to everybody.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.shared.models.event import (
    EVENT_CATEGORIES,
    EVENT_PUBLISHED,
    VISIBILITY_PUBLIC,
    ROLE_ARTIST,
    ROLE_COHOST,
    RSVP_GOING,
    Event,
    EventParticipant,
    EventPiece,
    EventRsvp,
    EventSave,
)
from src.shared.models.social import Follow
from src.shared.models.user import User
from src.shared.utils.app_error import AppError

MAX_TITLE = 140

# Host FAQs (BACKEND_CHANGES B24).
MAX_FAQS = 10
MAX_FAQ_QUESTION = 200
MAX_FAQ_ANSWER = 1000
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
    faqs=None,
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
        # The location decides the zone; the host's phone is only a fallback (B28).
        timezone=_zone(latitude, longitude) or tz,
        venue_name=(venue_name or "").strip() or None,
        address=(address or "").strip() or None,
        city=city_from_address(address),
        latitude=latitude,
        longitude=longitude,
        capacity=_clean_capacity(capacity),
        faqs=validate_faqs(faqs),
    )
    db.add(event)
    if commit:
        db.commit()
        db.refresh(event)
    else:
        db.flush()
    return event


def _zone(latitude, longitude) -> Optional[str]:
    from src.shared.config.timezones import zone_for

    return zone_for(latitude, longitude)


def city_from_address(address: Optional[str]) -> Optional[str]:
    """The town in an address, for "Near me" (BACKEND_CHANGES B23): the part before the last,
    unless it has a digit in it (a postcode), as the app read it. "12 Elm St, Dallas, TX" is
    Dallas; "Dallas" alone or "1 Rue X, 75011 Paris" gives nothing rather than a guess."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if len(parts) < 2:
        return None
    city = parts[-2]
    return None if re.search(r"\d", city) else city[:120]


def validate_title(title: Optional[str]) -> str:
    cleaned = (title or "").strip()
    if not cleaned:
        raise AppError("An event needs a title.", 400)
    if len(cleaned) > MAX_TITLE:
        raise AppError(f"An event title can be at most {MAX_TITLE} characters.", 400)
    return cleaned


def validate_faqs(raw) -> Optional[list]:
    """The host's FAQs as stored (B24): `[{"question", "answer"}]` in the order given, each
    trimmed, or None for none. A row left wholly blank (an editor's empty last row) is dropped;
    an answer without its question is refused, as is anything over the limits."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise AppError("faqs must be a list of questions and answers.", 400)
    faqs = []
    for item in raw:
        if not isinstance(item, dict):
            raise AppError("Each FAQ needs a question and an answer.", 400)
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if not question and not answer:
            continue
        if not question:
            raise AppError("Each FAQ needs a question.", 400)
        if len(question) > MAX_FAQ_QUESTION:
            raise AppError(f"An FAQ question can be at most {MAX_FAQ_QUESTION} characters.", 400)
        if len(answer) > MAX_FAQ_ANSWER:
            raise AppError(f"An FAQ answer can be at most {MAX_FAQ_ANSWER} characters.", 400)
        faqs.append({"question": question, "answer": answer})
    if len(faqs) > MAX_FAQS:
        raise AppError(f"An event can have at most {MAX_FAQS} FAQs.", 400)
    return faqs or None


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
    # Link-only events open from their link but are never listed (B28).
    return select(Event).where(
        Event.status == EVENT_PUBLISHED, Event.ends_at > now, Event.visibility == VISIBILITY_PUBLIC
    )


class EventFilter:
    """What the Events tab narrows by (BACKEND_CHANGES B23): a range, a city and a search.

    The range is an overlap: an event shows if it starts before `end` and ends after `start`,
    so a run already open when the range begins is in it. Search is over the title, venue,
    category and the host's name and username, case-insensitive.
    """

    def __init__(
        self,
        *,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        city: Optional[str] = None,
        q: Optional[str] = None,
    ):
        self.start = start
        self.end = end
        self.city = (city or "").strip() or None
        self.q = (q or "").strip() or None

    def apply(self, stmt):
        if self.start is not None:
            stmt = stmt.where(Event.ends_at > self.start)
        if self.end is not None:
            stmt = stmt.where(Event.starts_at < self.end)
        if self.city:
            stmt = stmt.where(func.lower(Event.city) == self.city.lower())
        if self.q:
            from src.modules.chat.chat_dao import contains_pattern

            pattern = contains_pattern(self.q)
            hosts = select(User.id).where(
                or_(User.name.ilike(pattern), User.username.ilike(pattern))
            )
            stmt = stmt.where(
                or_(
                    Event.title.ilike(pattern),
                    Event.venue_name.ilike(pattern),
                    Event.category.ilike(pattern),
                    Event.host_id.in_(hosts),
                )
            )
        return stmt


NO_FILTER = EventFilter()


def list_upcoming(
    db: Session,
    *,
    category: Optional[str] = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    where: EventFilter = NO_FILTER,
) -> list[Event]:
    stmt = where.apply(_published_upcoming(datetime.now(timezone.utc)))
    if category:
        stmt = stmt.where(Event.category == validate_category(category))
    return list(
        db.execute(
            stmt.order_by(Event.starts_at.asc()).limit(_clamp(limit)).offset(max(offset, 0))
        ).scalars()
    )


def list_today(
    db: Session, *, limit: int = DEFAULT_LIMIT, where: EventFilter = NO_FILTER
) -> list[Event]:
    """Happening within the next 24 hours, including anything already under way."""
    now = datetime.now(timezone.utc)
    return list(
        db.execute(
            where.apply(_published_upcoming(now))
            .where(Event.starts_at <= now + timedelta(hours=24))
            .order_by(Event.starts_at.asc())
            .limit(_clamp(limit))
        ).scalars()
    )


def list_from_following(
    db: Session, viewer_id: uuid.UUID, *, limit: int = DEFAULT_LIMIT, where: EventFilter = NO_FILTER
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
            where.apply(_published_upcoming(now))
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
        stmt = stmt.where(Event.status == EVENT_PUBLISHED, Event.visibility == VISIBILITY_PUBLIC)
    return list(db.execute(stmt.order_by(Event.starts_at.desc())).scalars())


def list_going(db: Session, viewer_id: uuid.UUID, *, limit: int = DEFAULT_LIMIT) -> list[Event]:
    """Events this viewer has RSVP'd `going` to — past and upcoming both.

    Unlike the browse scopes, this deliberately isn't windowed to what's still ahead: a
    "Registered" list that drops an event the moment it starts would be useless to someone
    checking it from the door.
    """
    going = select(EventRsvp.event_id).where(
        EventRsvp.user_id == viewer_id, EventRsvp.status == RSVP_GOING
    )
    return list(
        db.execute(
            select(Event)
            .where(Event.status == EVENT_PUBLISHED, Event.id.in_(going))
            .order_by(Event.starts_at.desc())
            .limit(_clamp(limit))
        ).scalars()
    )


def featured(db: Session, *, where: EventFilter = NO_FILTER) -> Optional[Event]:
    """The team's pick (BACKEND_CHANGES B23): the soonest featured event in the range, place
    and search asked for, or None."""
    return db.execute(
        where.apply(_published_upcoming(datetime.now(timezone.utc)))
        .where(Event.featured_at.is_not(None))
        .order_by(Event.starts_at.asc())
        .limit(1)
    ).scalar_one_or_none()


def city_summaries(db: Session) -> list[dict]:
    """The cities with something coming up, for the Near me sheet: name and how many, by name.
    Grouped case-insensitively, under the spelling most events use."""
    now = datetime.now(timezone.utc)
    rows = db.execute(
        select(Event.city, func.count(Event.id))
        .where(Event.status == EVENT_PUBLISHED, Event.ends_at > now, Event.city.is_not(None),
               Event.visibility == VISIBILITY_PUBLIC)
        .group_by(Event.city)
    ).all()
    grouped: dict[str, dict] = {}
    for city, count in rows:
        entry = grouped.setdefault(city.lower(), {"city": city, "upcomingCount": 0, "_top": 0})
        entry["upcomingCount"] += count
        if count > entry["_top"]:
            entry["city"], entry["_top"] = city, count
    return [
        {"city": e["city"], "upcomingCount": e["upcomingCount"]}
        for e in sorted(grouped.values(), key=lambda e: e["city"].lower())
    ]


def category_summaries(db: Session, *, where: EventFilter = NO_FILTER) -> list[dict]:
    """Upcoming count and a cover image per category, for the browse tiles.

    The cover is borrowed from the soonest upcoming event in that category rather than being
    a fixed asset. A category with nothing in it has no tile at all, so there is never an
    image standing in for an empty section — and the artwork on the tile is always something
    actually happening.
    """
    now = datetime.now(timezone.utc)
    rows = db.execute(
        where.apply(
            select(Event.category, Event.cover_media_url, Event.starts_at).where(
                Event.status == EVENT_PUBLISHED, Event.ends_at > now, Event.category.is_not(None),
                Event.visibility == VISIBILITY_PUBLIC,
            )
        ).order_by(Event.category.asc(), Event.starts_at.asc())
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


def delete_event(db: Session, event: Event) -> None:
    """Remove the event row itself, once whatever it listed has already been taken down.

    Every child row — the lineup, RSVPs, saves, cohost/artist entries — has
    `ondelete="CASCADE"` on its `event_id`, so this one delete is enough; there is nothing
    left over for a caller to clean up by hand. Deleting a piece's own listing is a different,
    narrower operation (`pieces_dao.delete_piece`, a soft delete) and is never touched here —
    an event coming down never takes the artist's piece down with it.
    """
    db.delete(event)
    db.commit()


def _clamp(limit: int) -> int:
    return max(1, min(limit or DEFAULT_LIMIT, MAX_LIMIT))
