"""Auctions and payment holds — bids stop being bare rows and start carrying money.

Revision ID: 031_auctions_and_holds
Revises: 030_tiered_commission
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "031_auctions_and_holds"
down_revision: Union[str, None] = "030_tiered_commission"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- auctions -------------------------------------------------------------------------
    # Auction state moves off `pieces`. The piece keeps `listing_type` — it says how this work
    # sells — while the auction row owns everything about one run of one auction. Deliberately
    # NOT one-per-piece: the spec requires cancel-then-relist (tagging a piece to an event ends
    # its current auction and starts a new one), so a piece accumulates auction rows over time.
    op.create_table(
        "auctions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("piece_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seller_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(32), server_default="draft", nullable=False),
        sa.Column("starting_bid_cents", sa.Integer(), nullable=False),
        # Hidden from bidders, who only ever see met / not met.
        sa.Column("reserve_cents", sa.Integer(), nullable=True),
        # Null for an event auction, whose window comes from the event instead.
        sa.Column("duration_days", sa.Integer(), nullable=True),
        sa.Column("opens_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=True),
        # The one manual extension a seller is allowed.
        sa.Column("extended_once", sa.Boolean(), server_default=sa.false(), nullable=False),
        # Standalone auctions extend on a late bid; event auctions stop dead so the piece can
        # be handed over before the room empties.
        sa.Column("soft_close_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        # Set in phase 8 once events exist. Kept nullable and unconstrained until then.
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("delivery_mode", sa.String(16), nullable=True),
        # Snapshotted when the auction is listed: an artist who lists under one rate must not
        # be paid under another because the platform changed it mid-auction.
        sa.Column("commission_bps", sa.Integer(), nullable=False),
        sa.Column("cancelled_reason", sa.String(64), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["piece_id"], ["pieces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["seller_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('draft','live','closing','closed_sold','closed_reserve_not_met',"
            "'closed_no_bids','needs_seller_action','cancelled')",
            name="ck_auctions_status",
        ),
        sa.CheckConstraint("starting_bid_cents >= 100", name="ck_auctions_starting_bid_min"),
        sa.CheckConstraint(
            "reserve_cents IS NULL OR reserve_cents >= starting_bid_cents",
            name="ck_auctions_reserve_above_start",
        ),
        sa.CheckConstraint(
            "duration_days IS NULL OR duration_days BETWEEN 3 AND 14",
            name="ck_auctions_duration_range",
        ),
        sa.CheckConstraint(
            "delivery_mode IS NULL OR delivery_mode IN ('ship','pickup')",
            name="ck_auctions_delivery_mode",
        ),
        sa.CheckConstraint(
            "commission_bps BETWEEN 0 AND 10000", name="ck_auctions_commission_bps_range"
        ),
    )
    op.create_index(op.f("ix_auctions_piece_id"), "auctions", ["piece_id"], unique=False)
    op.create_index(op.f("ix_auctions_seller_id"), "auctions", ["seller_id"], unique=False)
    # The close sweep's query: live auctions past their end time.
    op.create_index(
        op.f("ix_auctions_status_closes_at"), "auctions", ["status", "closes_at"], unique=False
    )
    # One *running* auction per piece. A partial index rather than a plain unique constraint,
    # so the closed and cancelled history can accumulate alongside it.
    op.create_index(
        "uq_auctions_one_running_per_piece",
        "auctions",
        ["piece_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('draft','live','closing')"),
    )

    # --- bids, rebuilt against auctions ---------------------------------------------------
    # Dropped rather than migrated: nothing has shipped, and every row would need an auction
    # to belong to that does not exist.
    op.drop_table("bids")
    op.create_table(
        "bids",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("auction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bidder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["auction_id"], ["auctions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bidder_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('active','outbid','cancelled','won','lost')", name="ck_bids_status"
        ),
        sa.CheckConstraint("amount_cents > 0", name="ck_bids_amount_positive"),
    )
    op.create_index(op.f("ix_bids_auction_id"), "bids", ["auction_id"], unique=False)
    op.create_index(op.f("ix_bids_bidder_id"), "bids", ["bidder_id"], unique=False)
    # Every hot read filters on this pair: the current high bid, the active count.
    op.create_index(
        op.f("ix_bids_auction_id_status"), "bids", ["auction_id", "status"], unique=False
    )

    # --- holds ----------------------------------------------------------------------------
    # The money behind a bid. Separate from `bids` because a bid is a historical fact that
    # never changes, while a hold has a lifecycle, can be refreshed, and can outlive or
    # predecease the bid it belongs to.
    op.create_table(
        "holds",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bid_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Denormalised so the refresh sweep can find a bidder's holds without joining.
        sa.Column("bidder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), server_default="pending", nullable=False),
        sa.Column("stripe_payment_intent_id", sa.String(255), nullable=True),
        sa.Column("stripe_payment_method_id", sa.String(255), nullable=True),
        # Read from Stripe, never assumed. Networks differ and the window can change.
        sa.Column("capture_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["bid_id"], ["bids.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bidder_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        # One Stripe intent is one hold. This is the duplicate-authorisation guard.
        sa.UniqueConstraint("stripe_payment_intent_id", name="uq_holds_stripe_intent"),
        sa.UniqueConstraint("bid_id", name="uq_holds_bid_id"),
        sa.CheckConstraint(
            "status IN ('pending','held','captured','released','capture_failed','expired','failed')",
            name="ck_holds_status",
        ),
        sa.CheckConstraint("amount_cents > 0", name="ck_holds_amount_positive"),
    )
    op.create_index(op.f("ix_holds_bidder_id"), "holds", ["bidder_id"], unique=False)
    # The nightly refresh sweep: held authorisations nearing expiry.
    op.create_index(
        op.f("ix_holds_status_capture_before"), "holds", ["status", "capture_before"], unique=False
    )

    # --- the piece keeps only its sale mode -------------------------------------------------
    op.drop_constraint("ck_pieces_auction_fields", "pieces", type_="check")
    op.drop_constraint("ck_pieces_auction_duration_range", "pieces", type_="check")
    op.drop_column("pieces", "auction_duration_days")
    op.drop_column("pieces", "auction_ends_at")

    # A saved card, so the refresh sweep can re-authorise off-session while the bidder sleeps.
    op.add_column("users", sa.Column("stripe_customer_id", sa.String(255), nullable=True))
    op.create_unique_constraint("uq_users_stripe_customer_id", "users", ["stripe_customer_id"])


def downgrade() -> None:
    op.drop_constraint("uq_users_stripe_customer_id", "users", type_="unique")
    op.drop_column("users", "stripe_customer_id")
    op.add_column("pieces", sa.Column("auction_ends_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("pieces", sa.Column("auction_duration_days", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_pieces_auction_duration_range", "pieces",
        "auction_duration_days IS NULL OR auction_duration_days BETWEEN 3 AND 14",
    )
    op.create_check_constraint(
        "ck_pieces_auction_fields", "pieces",
        "listing_type = 'auction' OR (auction_duration_days IS NULL AND auction_ends_at IS NULL)",
    )
    op.drop_table("holds")
    op.drop_table("bids")
    op.drop_index("uq_auctions_one_running_per_piece", table_name="auctions")
    op.drop_table("auctions")
    op.create_table(
        "bids",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("piece_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bidder_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancellation_reason", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["piece_id"], ["pieces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["bidder_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
