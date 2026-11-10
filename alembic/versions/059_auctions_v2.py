"""Auctions v2, Phase 7a: a card check instead of holds, the charge at close, the 12-hour fix
window and the second-chance offer (the 9 Nov design; BACKEND_CHANGES B20).

Revision ID: 059_auctions_v2
Revises: 058_collection_hidden

* bids.payment_method_id: the saved card the bidder verified (a SetupIntent, no hold). The
  winner's is charged off-session when the auction ends. bids.address_id: where it ships
  (the design's Ship to row), for the shipping and tax that join the charge in Phase 7b.
* auctions: the fix window starts once and never moves (BE-4), so a retry can't stretch it;
  `payment_attempts` and `next_payment_retry_at` drive the automatic retries. A new status,
  `second_chance`, while a runner-up is being offered the piece.
* auction_offers: the second-chance offer to the next bidder at their top bid.

Auctions already running on holds keep working: their winners are still captured from the
hold, and their runners-up cascade as before.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "059_auctions_v2"
down_revision: Union[str, None] = "058_collection_hidden"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)

OLD_STATUSES = ("'draft','live','closing','awaiting_payment','awaiting_winner','closed_sold',"
                "'closed_reserve_not_met','closed_no_bids','needs_seller_action','cancelled'")
NEW_STATUSES = OLD_STATUSES + ",'second_chance'"


def upgrade() -> None:
    op.add_column("bids", sa.Column("payment_method_id", sa.String(255), nullable=True))
    op.add_column("bids", sa.Column("address_id", UUID,
                                    sa.ForeignKey("addresses.id", ondelete="SET NULL"), nullable=True))

    op.add_column("auctions", sa.Column("payment_attempts", sa.Integer, nullable=False,
                                        server_default="0"))
    op.add_column("auctions", sa.Column("next_payment_retry_at", sa.DateTime(timezone=True),
                                        nullable=True))
    op.drop_constraint("ck_auctions_status", "auctions", type_="check")
    op.create_check_constraint("ck_auctions_status", "auctions", f"status IN ({NEW_STATUSES})")

    op.create_table(
        "auction_offers",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("auction_id", UUID, sa.ForeignKey("auctions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bid_id", UUID, sa.ForeignKey("bids.id", ondelete="CASCADE"), nullable=False),
        sa.Column("bidder_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("bid_id", name="uq_auction_offers_bid"),
        sa.CheckConstraint("status IN ('pending','accepted','declined','expired')",
                           name="ck_auction_offers_status"),
        sa.CheckConstraint("amount_cents > 0", name="ck_auction_offers_amount"),
    )
    op.create_index("ix_auction_offers_auction_status", "auction_offers", ["auction_id", "status"])
    op.create_index("ix_auction_offers_bidder", "auction_offers", ["bidder_id"])


def downgrade() -> None:
    op.drop_index("ix_auction_offers_bidder", table_name="auction_offers")
    op.drop_index("ix_auction_offers_auction_status", table_name="auction_offers")
    op.drop_table("auction_offers")
    op.execute("UPDATE auctions SET status = 'needs_seller_action' WHERE status = 'second_chance'")
    op.drop_constraint("ck_auctions_status", "auctions", type_="check")
    op.create_check_constraint("ck_auctions_status", "auctions", f"status IN ({OLD_STATUSES})")
    op.drop_column("auctions", "next_payment_retry_at")
    op.drop_column("auctions", "payment_attempts")
    op.drop_column("bids", "address_id")
    op.drop_column("bids", "payment_method_id")
