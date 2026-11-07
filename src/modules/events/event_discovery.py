"""Events outside the Events tab (BACKEND_CHANGES B27): the one event card in the home feed,
Explore's Upcoming shows, and a host's upcoming show on their profile.

Every query here starts from what a public listing may show — published and not yet ended —
so a draft, a cancelled event or one that is over can never reach a feed, a shelf or a
profile.
"""
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.modules.pieces import piece_state
from src.shared.models.event import EVENT_PUBLISHED, Event, EventPiece
from src.shared.models.piece import Piece
from src.shared.models.social import Follow
from src.shared.models.user import User

# The home feed's event: one starting in the next two weeks (`NoteMain`, `HandoffBrowse`).
FEED_WINDOW = timedelta(days=14)
# How many of the soonest events the feed picks among.
FEED_CANDIDATES = 50
# "Near the viewer": within this distance of the location on their account.
NEAR_KM = 80.0
# Where the event goes in a feed page: after the third item (`NoteMain`).
FEED_SLOT = 3

# Explore's Upcoming shows: the soonest shows with work on their bill, and up to this many of
# each one's pieces (the shelf is the pieces; its subtitle is the show).
SHOWS_LIMIT = 5
SHOW_PIECES = 12

HOST_UPCOMING_LIMIT = 20


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _upcoming(now: datetime):
    return select(Event).where(Event.status == EVENT_PUBLISHED, Event.ends_at > now)


# --- profiles -----------------------------------------------------------------------------------

def list_host_upcoming(
    db: Session, host_id: uuid.UUID, *, limit: int = HOST_UPCOMING_LIMIT
) -> list[Event]:
    """A host's published events that haven't ended, soonest first."""
    return list(
        db.execute(
            _upcoming(_now())
            .where(Event.host_id == host_id)
            .order_by(Event.starts_at.asc(), Event.id.asc())
            .limit(max(1, min(limit, 50)))
        ).scalars()
    )


def upcoming_for_host(db: Session, host_id: uuid.UUID) -> Optional[Event]:
    """The profile's Upcoming show: the host's soonest published event still to come."""
    found = list_host_upcoming(db, host_id, limit=1)
    return found[0] if found else None


# --- the home feed ------------------------------------------------------------------------------

def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance (haversine)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def rank_feed_events(
    events: Iterable[Event],
    *,
    followed: set,
    viewer_lat: Optional[float] = None,
    viewer_lng: Optional[float] = None,
) -> list[Event]:
    """Events by how well they fit the viewer's feed: a host they follow first, then one near
    the location on their account, then the rest; soonest first within each."""
    def near(e: Event) -> bool:
        if None in (viewer_lat, viewer_lng, e.latitude, e.longitude):
            return False
        return distance_km(viewer_lat, viewer_lng, e.latitude, e.longitude) <= NEAR_KM

    def key(e: Event):
        group = 0 if e.host_id in followed else (1 if near(e) else 2)
        return (group, e.starts_at, str(e.id))

    return sorted(events, key=key)


def feed_slot(count: int) -> int:
    """Where the event goes in a page of `count` items: after the third, or at the end of a
    shorter page; -1 for an empty page (an event alone is not a feed)."""
    if count <= 0:
        return -1
    return min(FEED_SLOT, count)


def feed_event(
    db: Session, viewer_id: Optional[uuid.UUID], *, hidden: Optional[set] = None
) -> Optional[Event]:
    """The one event for this viewer's feed: starting within two weeks and not over, from a
    host they haven't blocked (or been blocked by)."""
    now = _now()
    stmt = _upcoming(now).where(Event.starts_at < now + FEED_WINDOW)
    if hidden:
        stmt = stmt.where(Event.host_id.notin_(hidden))
    candidates = list(
        db.execute(stmt.order_by(Event.starts_at.asc()).limit(FEED_CANDIDATES)).scalars()
    )
    if not candidates:
        return None
    followed: set = set()
    lat = lng = None
    if viewer_id is not None:
        followed = set(
            db.execute(
                select(Follow.following_id).where(
                    Follow.follower_id == viewer_id, Follow.status == "accepted"
                )
            ).scalars()
        )
        viewer = db.get(User, viewer_id)
        if viewer is not None:
            lat, lng = viewer.latitude, viewer.longitude
    return rank_feed_events(candidates, followed=followed, viewer_lat=lat, viewer_lng=lng)[0]


# --- Explore ------------------------------------------------------------------------------------

def upcoming_shows(
    db: Session, *, hidden: Optional[set] = None, limit: int = SHOWS_LIMIT
) -> list[tuple[Event, list[dict]]]:
    """The soonest published shows still to come that have work on their bill, each with up to
    `SHOW_PIECES` of its pieces in bill order. Shows whose bill is empty are left out: the
    shelf is the pieces."""
    now = _now()
    with_work = select(EventPiece.event_id)
    stmt = _upcoming(now).where(Event.id.in_(with_work))
    if hidden:
        stmt = stmt.where(Event.host_id.notin_(hidden))
    events = list(
        db.execute(stmt.order_by(Event.starts_at.asc(), Event.id.asc()).limit(limit)).scalars()
    )
    shows = []
    for event in events:
        rows = db.execute(
            select(EventPiece, Piece, User)
            .join(Piece, Piece.id == EventPiece.piece_id)
            .join(User, User.id == Piece.user_id)
            .where(
                EventPiece.event_id == event.id,
                Piece.deleted_at.is_(None),
                Piece.status.in_(piece_state.PUBLIC_STATUSES),
            )
            .order_by(EventPiece.sort_order.asc(), EventPiece.created_at.asc())
            .limit(SHOW_PIECES)
        ).all()
        pieces = [
            {
                "pieceId": str(piece.id),
                "title": piece.title,
                "mediaUrl": piece.media_url,
                "mode": entry.mode,
                "priceCents": entry.price_cents,
                "artistUsername": artist.username,
                "artistName": artist.name,
            }
            for entry, piece, artist in rows
            if not hidden or artist.id not in hidden
        ]
        if pieces:
            shows.append((event, pieces))
    return shows
