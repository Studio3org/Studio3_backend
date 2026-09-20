"""Events — a gathering, the people on its bill, and the work shown there.

Split across four tables, and the split follows who owns what:

* **Event** is the gathering: when, where, who is hosting, and whether it has been published.
* **EventParticipant** is the bill — cohosts and artists. A row here is a credit, not a
  permission: tagging an artist does not hand the host any authority over their work.
* **EventPiece** is the lineup. It carries *how* a piece appears — shown, sold, or auctioned
  — because that differs per event and must not be written back onto the piece itself.
* **EventSave** is the bookmark, kept separate so saving is a cheap insert rather than a
  write to the event row every collector touches.
* **EventRsvp** is "I'm coming". Distinct from a save: saving is interest, an RSVP is a
  headcount the host plans a room around.

Entry is free and open by design: attending is not gated, and registering exists to let
someone transact, not to let them in. There are deliberately no ticket or price columns here
— paid ticketing is deferred, and modelling a price nothing reads would invite code that
half-supports it.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


# --- event status -------------------------------------------------------------------------
EVENT_DRAFT = "draft"
EVENT_PUBLISHED = "published"
EVENT_CANCELLED = "cancelled"
# Past and settled. Kept visible on a profile as history rather than deleted.
EVENT_ARCHIVED = "archived"

EVENT_STATUSES = (EVENT_DRAFT, EVENT_PUBLISHED, EVENT_CANCELLED, EVENT_ARCHIVED)
# Statuses in which an event appears in public listings.
EVENT_PUBLIC_STATUSES = (EVENT_PUBLISHED, EVENT_ARCHIVED)

# The union of what a host can pick in the create flow and what a browser can filter by on
# the events tab — both are real surfaces in the app, and each offers categories the other
# does not. Anything outside this set is refused rather than stored, so a typo cannot create
# a category nothing lists under.
EVENT_CATEGORIES = (
    # Offered by the create flow's category picker.
    "workshop",
    "gallery_walk",
    "exhibition",
    "talks_panels",
    "demos_performances",
    # Offered as browse tiles.
    "studio_visit",
    "popup",
    "market",
    "auction",
    "other",
)

# --- participant roles --------------------------------------------------------------------
ROLE_COHOST = "cohost"
ROLE_ARTIST = "artist"
PARTICIPANT_ROLES = (ROLE_COHOST, ROLE_ARTIST)

# --- lineup modes -------------------------------------------------------------------------
# Shown only. Nothing about the piece's own listing changes.
PIECE_FEATURED = "featured"
# Sold at a fixed price at the event.
PIECE_SALE = "sale"
# Auctioned in the room, on the event's clock.
PIECE_BID = "bid"
EVENT_PIECE_MODES = (PIECE_FEATURED, PIECE_SALE, PIECE_BID)

# Modes that put a piece on sale, and therefore end whatever listing it already had.
SELLING_MODES = (PIECE_SALE, PIECE_BID)

DELIVERY_SHIP = "ship"
DELIVERY_PICKUP = "pickup"


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','published','cancelled','archived')", name="ck_events_status"
        ),
        # An event that ends before it starts would break every window derived from it —
        # most sharply the event auction, whose close is computed backwards from ends_at.
        CheckConstraint("ends_at > starts_at", name="ck_events_ends_after_starts"),
        CheckConstraint(
            "latitude IS NULL OR (latitude BETWEEN -90 AND 90)", name="ck_events_latitude"
        ),
        CheckConstraint(
            "longitude IS NULL OR (longitude BETWEEN -180 AND 180)", name="ck_events_longitude"
        ),
        CheckConstraint("capacity IS NULL OR capacity > 0", name="ck_events_capacity_positive"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    title = Column(String(140), nullable=False)
    description = Column(Text, nullable=True)
    cover_media_url = Column(String(1024), nullable=True)
    category = Column(String(32), nullable=True)

    # Stored in UTC, always. `timezone` is the IANA zone the host chose and exists only so
    # the app can render "8:00 PM CST" rather than guessing from the reader's device — an
    # event happens in one place, and its time should read the same to everyone.
    starts_at = Column(DateTime(timezone=True), nullable=False, index=True)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    timezone = Column(String(64), nullable=True)

    venue_name = Column(String(200), nullable=True)
    address = Column(String(500), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)

    # Null means unlimited, which is the default and the common case — entry is free and
    # open. A number caps RSVPs; the waitlist that turns "full" into a queue is deferred.
    capacity = Column(Integer, nullable=True)

    status = Column(String(16), default=EVENT_DRAFT, server_default=EVENT_DRAFT, nullable=False)
    published_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancellation_reason = Column(String(200), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    @property
    def is_published(self) -> bool:
        return self.status == EVENT_PUBLISHED

    @property
    def is_over(self) -> bool:
        return self.ends_at is not None and self.ends_at <= datetime.now(timezone.utc)


class EventParticipant(Base):
    """A cohost or a credited artist.

    Explicitly **not** a grant of authority. A host may credit any artist on the bill, but
    that does not let the host sell that artist's work or end their listings — see
    lineup_service for where that line is drawn and why.
    """

    __tablename__ = "event_participants"
    __table_args__ = (
        UniqueConstraint("event_id", "user_id", "role", name="uq_event_participant"),
        CheckConstraint("role IN ('cohost','artist')", name="ck_event_participants_role"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role = Column(String(16), nullable=False)
    sort_order = Column(Integer, default=0, server_default="0", nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class EventPiece(Base):
    """One work on the bill, and how it appears there.

    `mode` lives here rather than on the piece because it is a fact about this event: the
    same work can be shown at one and auctioned at the next, and writing it onto the piece
    would make the last event win.

    `price_cents` is the fixed price for a `sale` and the starting bid for a `bid`. One
    column, because they are the same thing from the piece's point of view — the number the
    artist is asking — and two would immediately drift.
    """

    __tablename__ = "event_pieces"
    __table_args__ = (
        UniqueConstraint("event_id", "piece_id", name="uq_event_piece"),
        CheckConstraint("mode IN ('featured','sale','bid')", name="ck_event_pieces_mode"),
        CheckConstraint(
            "delivery_mode IS NULL OR delivery_mode IN ('ship','pickup')",
            name="ck_event_pieces_delivery_mode",
        ),
        # A featured piece carries no price; a selling one must.
        CheckConstraint(
            "(mode = 'featured') OR (price_cents IS NOT NULL AND price_cents >= 100)",
            name="ck_event_pieces_price_when_selling",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    piece_id = Column(
        UUID(as_uuid=True), ForeignKey("pieces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mode = Column(String(16), default=PIECE_FEATURED, server_default=PIECE_FEATURED, nullable=False)
    price_cents = Column(Integer, nullable=True)
    # Pickup is the norm at an event: the piece changes hands in the room.
    delivery_mode = Column(String(16), nullable=True)
    sort_order = Column(Integer, default=0, server_default="0", nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)

    @property
    def is_selling(self) -> bool:
        return self.mode in SELLING_MODES


class EventSave(Base):
    """A collector's bookmark."""

    __tablename__ = "event_saves"
    __table_args__ = (UniqueConstraint("event_id", "user_id", name="uq_event_save"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


# --- rsvp status ----------------------------------------------------------------------------
RSVP_GOING = "going"
# Kept rather than deleted: "said yes then pulled out" is a different fact from "never
# answered", and a host reading a headcount the morning of should be able to tell them apart.
RSVP_CANCELLED = "cancelled"
RSVP_STATUSES = (RSVP_GOING, RSVP_CANCELLED)


class EventRsvp(Base):
    """Someone saying they will be there.

    Deliberately not a ticket. Entry is free and open, so this does not admit anyone and
    nothing is charged for it — it is a headcount the host plans a room around, and the
    thing that makes "the event you're going to was cancelled" a message we can actually
    send.

    One row per person per event, flipped between `going` and `cancelled`, rather than
    inserted and deleted. That keeps the unique constraint meaningful, makes re-RSVPing a
    status change instead of a race, and preserves the difference between pulling out and
    never replying.
    """

    __tablename__ = "event_rsvps"
    __table_args__ = (
        UniqueConstraint("event_id", "user_id", name="uq_event_rsvp"),
        CheckConstraint("status IN ('going','cancelled')", name="ck_event_rsvps_status"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status = Column(String(16), default=RSVP_GOING, server_default=RSVP_GOING, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
