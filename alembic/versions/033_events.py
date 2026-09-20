"""Events: the gathering, its bill, its lineup, and its bookmarks.

Revision ID: 033_events
Revises: 032_auction_winner_cascade

Four tables rather than one wide one, because they have different owners and different write
rates: the event row is written by its host, participants by the host, lineup entries by the
artists whose work they are, and saves by every collector who taps a bookmark.

Also closes a loose end from 031: `auctions.event_id` has existed since event auctions were
designed, as a bare UUID with nothing to point at. Now that `events` exists it gets its real
foreign key.

Deliberately absent: tickets and prices. Entry is free and open, paid ticketing is deferred,
and a price column nothing reads is an invitation to write code that half-supports one.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "033_events"
down_revision: Union[str, None] = "032_auction_winner_cascade"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- events -----------------------------------------------------------------------------
    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("host_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(140), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("cover_media_url", sa.String(1024), nullable=True),
        sa.Column("category", sa.String(32), nullable=True),
        # UTC. `timezone` is the IANA zone the host chose, kept so the app can render the
        # event's own local time rather than the reader's — an event happens in one place.
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("venue_name", sa.String(200), nullable=True),
        sa.Column("address", sa.String(500), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), server_default="draft", nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # RESTRICT: an event is a public record other people attended and listed work at.
        # Deleting the host out from under it would orphan every lineup entry.
        sa.ForeignKeyConstraint(["host_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('draft','published','cancelled','archived')", name="ck_events_status"
        ),
        # An event auction's close is computed backwards from ends_at, so an event that ends
        # before it starts would produce an auction that closes before it opens.
        sa.CheckConstraint("ends_at > starts_at", name="ck_events_ends_after_starts"),
        sa.CheckConstraint(
            "latitude IS NULL OR (latitude BETWEEN -90 AND 90)", name="ck_events_latitude"
        ),
        sa.CheckConstraint(
            "longitude IS NULL OR (longitude BETWEEN -180 AND 180)", name="ck_events_longitude"
        ),
    )
    op.create_index(op.f("ix_events_host_id"), "events", ["host_id"], unique=False)
    op.create_index(op.f("ix_events_starts_at"), "events", ["starts_at"], unique=False)
    # The listing query: published events from now forwards, soonest first. Partial, because
    # every other status is invisible to it and past events grow without bound.
    op.create_index(
        "ix_events_published_upcoming",
        "events",
        ["starts_at"],
        postgresql_where=sa.text("status = 'published'"),
    )

    # --- participants -----------------------------------------------------------------------
    op.create_table(
        "event_participants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "user_id", "role", name="uq_event_participant"),
        sa.CheckConstraint("role IN ('cohost','artist')", name="ck_event_participants_role"),
    )
    op.create_index(
        op.f("ix_event_participants_event_id"), "event_participants", ["event_id"], unique=False
    )
    op.create_index(
        op.f("ix_event_participants_user_id"), "event_participants", ["user_id"], unique=False
    )

    # --- lineup -----------------------------------------------------------------------------
    op.create_table(
        "event_pieces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("piece_id", postgresql.UUID(as_uuid=True), nullable=False),
        # How the work appears at *this* event. On the join row rather than the piece,
        # because the same work can be shown at one event and auctioned at the next.
        sa.Column("mode", sa.String(16), server_default="featured", nullable=False),
        # The fixed price for a sale, the starting bid for an auction. One column: from the
        # piece's side they are the same number — what the artist is asking — and two would
        # drift within a release.
        sa.Column("price_cents", sa.Integer(), nullable=True),
        sa.Column("delivery_mode", sa.String(16), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["piece_id"], ["pieces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "piece_id", name="uq_event_piece"),
        sa.CheckConstraint("mode IN ('featured','sale','bid')", name="ck_event_pieces_mode"),
        sa.CheckConstraint(
            "delivery_mode IS NULL OR delivery_mode IN ('ship','pickup')",
            name="ck_event_pieces_delivery_mode",
        ),
        sa.CheckConstraint(
            "(mode = 'featured') OR (price_cents IS NOT NULL AND price_cents >= 100)",
            name="ck_event_pieces_price_when_selling",
        ),
    )
    op.create_index(
        op.f("ix_event_pieces_event_id"), "event_pieces", ["event_id"], unique=False
    )
    op.create_index(
        op.f("ix_event_pieces_piece_id"), "event_pieces", ["piece_id"], unique=False
    )

    # --- saves ------------------------------------------------------------------------------
    op.create_table(
        "event_saves",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "user_id", name="uq_event_save"),
    )
    op.create_index(op.f("ix_event_saves_event_id"), "event_saves", ["event_id"], unique=False)
    op.create_index(op.f("ix_event_saves_user_id"), "event_saves", ["user_id"], unique=False)

    # --- the auction link finally has somewhere to point ------------------------------------
    # SET NULL rather than CASCADE: deleting an event must never delete the auction rows that
    # recorded who bid what, which are the dispute record for money that actually moved.
    op.create_foreign_key(
        "fk_auctions_event_id", "auctions", "events", ["event_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index(op.f("ix_auctions_event_id"), "auctions", ["event_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_auctions_event_id"), table_name="auctions")
    op.drop_constraint("fk_auctions_event_id", "auctions", type_="foreignkey")

    op.drop_table("event_saves")
    op.drop_table("event_pieces")
    op.drop_table("event_participants")
    op.drop_index("ix_events_published_upcoming", table_name="events")
    op.drop_table("events")
