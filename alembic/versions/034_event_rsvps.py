"""RSVPs, and an optional cap on how many an event takes.

Revision ID: 034_event_rsvps
Revises: 033_events

An RSVP is not a ticket. Entry is free and open, so this admits nobody and charges nothing —
it is a headcount the host plans a room around, and the record that makes "the event you
were going to has been cancelled" a message we can actually send.

`capacity` is nullable and that is the norm: null means unlimited. A number caps RSVPs and a
full event refuses further ones, which is the state the deferred waitlist will later turn
into a queue rather than a rejection.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "034_event_rsvps"
down_revision: Union[str, None] = "033_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("events", sa.Column("capacity", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_events_capacity_positive", "events", "capacity IS NULL OR capacity > 0"
    )

    op.create_table(
        "event_rsvps",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Flipped between 'going' and 'cancelled' rather than inserted and deleted: that
        # keeps the unique constraint meaningful, makes re-RSVPing a status change instead of
        # a race against a delete, and preserves the difference between somebody who pulled
        # out and somebody who never replied.
        sa.Column("status", sa.String(16), server_default="going", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", "user_id", name="uq_event_rsvp"),
        sa.CheckConstraint("status IN ('going','cancelled')", name="ck_event_rsvps_status"),
    )
    op.create_index(op.f("ix_event_rsvps_event_id"), "event_rsvps", ["event_id"], unique=False)
    op.create_index(op.f("ix_event_rsvps_user_id"), "event_rsvps", ["user_id"], unique=False)
    # The headcount query, and the list the cancellation notice walks. Partial, because a
    # cancelled RSVP is history and never counted or messaged.
    op.create_index(
        "ix_event_rsvps_going",
        "event_rsvps",
        ["event_id"],
        postgresql_where=sa.text("status = 'going'"),
    )


def downgrade() -> None:
    op.drop_index("ix_event_rsvps_going", table_name="event_rsvps")
    op.drop_table("event_rsvps")
    op.drop_constraint("ck_events_capacity_positive", "events", type_="check")
    op.drop_column("events", "capacity")
