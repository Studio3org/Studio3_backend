"""Auction bidding: bids table + pieces.auction_ends_at.

Revision ID: 027_auction_bids
Revises: 026_reports
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "027_auction_bids"
down_revision: Union[str, None] = "026_reports"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("pieces", sa.Column("auction_ends_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "bids",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("piece_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bidder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["piece_id"], ["pieces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bidder_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_bids_piece_id"), "bids", ["piece_id"], unique=False)
    op.create_index(op.f("ix_bids_bidder_id"), "bids", ["bidder_id"], unique=False)


def downgrade() -> None:
    op.drop_table("bids")
    op.drop_column("pieces", "auction_ends_at")
