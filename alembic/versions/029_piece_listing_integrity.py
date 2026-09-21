"""Bid cancellation state + piece listing invariants as DB constraints.

Revision ID: 029_piece_listing_integrity
Revises: 028_profile_socials
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "029_piece_listing_integrity"
down_revision: Union[str, None] = "028_profile_socials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- bids gain a lifecycle ----------------------------------------------------------
    # Bids were immutable rows with no state, so a cancelled auction left them looking live
    # forever and get_highest_bid would resurrect them if the piece were relisted.
    op.add_column(
        "bids",
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
    )
    op.add_column("bids", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bids", sa.Column("cancellation_reason", sa.String(64), nullable=True))
    op.create_check_constraint(
        "ck_bids_status", "bids", "status IN ('active', 'cancelled', 'won')"
    )
    # Every hot read (highest bid, active count) filters on exactly this pair.
    op.create_index(op.f("ix_bids_piece_id_status"), "bids", ["piece_id", "status"], unique=False)

    # --- repair existing rows before constraining them -----------------------------------
    # listing_type arrived in 025 as a nullable column with no backfill, so every piece
    # listed before auctions existed still has NULL. They are all fixed-price by definition.
    op.execute(
        "UPDATE pieces SET listing_type = 'fixed' "
        "WHERE is_for_sale = true AND listing_type IS NULL"
    )
    # Auction columns on anything that is not an auction — a stale clock here makes a
    # relisted piece expire the moment it goes live.
    op.execute(
        "UPDATE pieces SET auction_duration_days = NULL, auction_ends_at = NULL "
        "WHERE listing_type IS DISTINCT FROM 'auction'"
    )
    op.execute(
        "UPDATE pieces SET auction_duration_days = NULL "
        "WHERE auction_duration_days IS NOT NULL "
        "AND (auction_duration_days < 3 OR auction_duration_days > 14)"
    )
    # A for-sale piece with no usable price cannot satisfy the shape check; park it as a
    # draft listing rather than dropping the row's price on the floor.
    op.execute(
        "UPDATE pieces SET is_for_sale = false, listing_type = NULL, "
        "auction_duration_days = NULL, auction_ends_at = NULL "
        "WHERE is_for_sale = true AND (price_cents IS NULL OR price_cents < 100)"
    )
    op.execute(
        "UPDATE pieces SET status = 'delisted' WHERE status NOT IN "
        "('draft', 'live', 'reserved', 'sold', 'auction_won', 'delisted', 'deleted')"
    )

    # --- piece invariants ----------------------------------------------------------------
    # These are a backstop for bugs, not user input validation: the app layer rejects all of
    # this first with a proper 4xx. Reaching one of these means a code path skipped
    # listing_rules, and failing loudly beats persisting an impossible listing.
    op.create_check_constraint(
        "ck_pieces_status",
        "pieces",
        "status IN ('draft', 'live', 'reserved', 'sold', 'auction_won', 'delisted', 'deleted')",
    )
    op.create_check_constraint(
        "ck_pieces_listing_type",
        "pieces",
        "listing_type IS NULL OR listing_type IN ('fixed', 'auction')",
    )
    op.create_check_constraint(
        "ck_pieces_sale_shape",
        "pieces",
        "is_for_sale = false OR (listing_type IS NOT NULL AND price_cents >= 100)",
    )
    op.create_check_constraint(
        "ck_pieces_auction_fields",
        "pieces",
        "listing_type = 'auction' OR "
        "(auction_duration_days IS NULL AND auction_ends_at IS NULL)",
    )
    op.create_check_constraint(
        "ck_pieces_auction_duration_range",
        "pieces",
        "auction_duration_days IS NULL OR auction_duration_days BETWEEN 3 AND 14",
    )


def downgrade() -> None:
    op.drop_constraint("ck_pieces_auction_duration_range", "pieces", type_="check")
    op.drop_constraint("ck_pieces_auction_fields", "pieces", type_="check")
    op.drop_constraint("ck_pieces_sale_shape", "pieces", type_="check")
    op.drop_constraint("ck_pieces_listing_type", "pieces", type_="check")
    op.drop_constraint("ck_pieces_status", "pieces", type_="check")
    op.drop_index(op.f("ix_bids_piece_id_status"), table_name="bids")
    op.drop_constraint("ck_bids_status", "bids", type_="check")
    op.drop_column("bids", "cancellation_reason")
    op.drop_column("bids", "cancelled_at")
    op.drop_column("bids", "status")
