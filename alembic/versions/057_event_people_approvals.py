"""Hosting, Phase 5: invites, piece approval, the bidding end, Exhibited, the 2-hour reminder.

Revision ID: 057_event_people_approvals
Revises: 056_ticketing_phase4

Built to the ticketing spec of 25 Oct 2026 ("Lineup", "Art at events", "Notifications") and
BACKEND_CHANGES B26/B28.

* event_participants.status: invited, accepted or declined. Co-hosts and featured artists
  must accept before they show on the event or can act on it. Existing rows predate invites
  and are kept as accepted.
* event_pieces.approval_status: waiting, approved or declined. Someone else's piece waits
  for its artist; existing rows are approved (they were the owner's, or featured only).
  `requested_by_id` is who tagged it; `listing_ended` records whether approving ended the
  piece's own listing (spec "ends listing (yes or no)").
* events.auction_ends_at: the host's chosen end of bidding, at the latest 30 minutes before
  the event ends. Null keeps today's rule (exactly 30 minutes before).
* event_rsvps.reminded_soon_at: the spec's second reminder, 2 hours before.
* piece_exhibitions: an Exhibited entry per tagged piece once an event is over (B26). The
  event's name, venue, city and date are copied, so the record survives the event's row.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "057_event_people_approvals"
down_revision: Union[str, None] = "056_ticketing_phase4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column("event_participants", sa.Column("status", sa.String(12), nullable=False,
                                                  server_default="accepted"))
    op.add_column("event_participants", sa.Column("invited_at", sa.DateTime(timezone=True),
                                                  nullable=True))
    op.add_column("event_participants", sa.Column("responded_at", sa.DateTime(timezone=True),
                                                  nullable=True))
    op.create_check_constraint("ck_event_participants_status", "event_participants",
                               "status IN ('invited','accepted','declined')")
    op.create_index("ix_event_participants_user_status", "event_participants",
                    ["user_id", "status"])

    op.add_column("event_pieces", sa.Column("approval_status", sa.String(12), nullable=False,
                                            server_default="approved"))
    op.add_column("event_pieces", sa.Column("requested_by_id", UUID,
                                            sa.ForeignKey("users.id", ondelete="SET NULL"),
                                            nullable=True))
    op.add_column("event_pieces", sa.Column("responded_at", sa.DateTime(timezone=True),
                                            nullable=True))
    op.add_column("event_pieces", sa.Column("listing_ended", sa.Boolean, nullable=False,
                                            server_default=sa.false()))
    op.create_check_constraint("ck_event_pieces_approval", "event_pieces",
                               "approval_status IN ('waiting','approved','declined')")

    op.add_column("events", sa.Column("auction_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("event_rsvps", sa.Column("reminded_soon_at", sa.DateTime(timezone=True),
                                           nullable=True))

    op.create_table(
        "piece_exhibitions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("piece_id", UUID, sa.ForeignKey("pieces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_id", UUID, sa.ForeignKey("events.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(140), nullable=False),
        sa.Column("venue_name", sa.String(200), nullable=True),
        sa.Column("city", sa.String(120), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("piece_id", "event_id", name="uq_piece_exhibition"),
    )
    op.create_index("ix_piece_exhibitions_piece", "piece_exhibitions", ["piece_id"])


def downgrade() -> None:
    op.drop_index("ix_piece_exhibitions_piece", table_name="piece_exhibitions")
    op.drop_table("piece_exhibitions")
    op.drop_column("event_rsvps", "reminded_soon_at")
    op.drop_column("events", "auction_ends_at")
    op.drop_constraint("ck_event_pieces_approval", "event_pieces", type_="check")
    for column in ("listing_ended", "responded_at", "requested_by_id", "approval_status"):
        op.drop_column("event_pieces", column)
    op.drop_index("ix_event_participants_user_status", table_name="event_participants")
    op.drop_constraint("ck_event_participants_status", "event_participants", type_="check")
    for column in ("responded_at", "invited_at", "status"):
        op.drop_column("event_participants", column)
