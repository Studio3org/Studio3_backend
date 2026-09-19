"""The winner cascade, and the money already collected before checkout.

Revision ID: 032_auction_winner_cascade
Revises: 031_auctions_and_holds

Two things land together because they are the same story told from either end.

**Who won** stops being derived and becomes a stored pointer. It used to be read back as
"the highest active bid", which broke the moment the close marked that bid `won` — the
status filter then excluded it and the auction had no winner at all. It also cannot express
a cascade: when the top bidder's card fails, the winner is the *second* highest, and no
query over bid amounts can say that.

**What the buyer has already paid** stops being assumed to be nothing. An auction captures
the winner's hold at close, so by the time they reach checkout the hammer price is already
taken. Without somewhere to record that, checkout priced the payment intent at the full
total and charged the artwork a second time.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "032_auction_winner_cascade"
down_revision: Union[str, None] = "031_auctions_and_holds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- auctions: who won, and how long they have ----------------------------------------
    op.add_column(
        "auctions",
        sa.Column("winning_bid_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # SET NULL rather than CASCADE: losing the bid row must not delete the auction that
    # recorded it. Bids are never hard-deleted today, so this is defensive.
    op.create_foreign_key(
        "fk_auctions_winning_bid_id",
        "auctions", "bids", ["winning_bid_id"], ["id"], ondelete="SET NULL",
    )
    # When the current winner's claim lapses and the piece passes to the next bidder.
    op.add_column(
        "auctions", sa.Column("winner_deadline_at", sa.DateTime(timezone=True), nullable=True)
    )
    # How many bidders have been passed over. Read by ops, and it bounds the cascade.
    op.add_column(
        "auctions",
        sa.Column("cascade_depth", sa.Integer(), server_default="0", nullable=False),
    )

    # The sweep's access pattern, and only for rows it can act on. A partial index keeps it
    # to the handful of auctions actually waiting on a winner rather than every auction ever
    # run.
    op.create_index(
        "ix_auctions_awaiting_winner_deadline",
        "auctions",
        ["winner_deadline_at"],
        postgresql_where=sa.text("status IN ('awaiting_payment','awaiting_winner')"),
    )

    # Two new statuses, and the difference between them is *whose money is still held*.
    #
    #   awaiting_payment — the winner's capture failed. They have until winner_deadline_at to
    #     fix it, and the runners-up holds are deliberately KEPT, because the cascade needs
    #     something to fall back to. Nothing has been taken from anyone.
    #   awaiting_winner  — the money is captured and the runners-up are released. The sale is
    #     settled; all that is outstanding is delivery details.
    #
    # Collapsing these into one status would lose the invariant that says whether a cascade
    # is still possible, which is the only thing that makes the fallback safe.
    op.drop_constraint("ck_auctions_status", "auctions", type_="check")
    op.create_check_constraint(
        "ck_auctions_status",
        "auctions",
        "status IN ('draft','live','closing','awaiting_payment','awaiting_winner',"
        "'closed_sold','closed_reserve_not_met','closed_no_bids','needs_seller_action',"
        "'cancelled')",
    )
    op.create_check_constraint(
        "ck_auctions_cascade_depth_positive", "auctions", "cascade_depth >= 0"
    )

    # --- bids: forfeited -------------------------------------------------------------------
    # `lost` means someone else won fairly. `forfeited` means this bidder won and did not
    # complete — a materially different fact, and the one the seller needs to see.
    op.drop_constraint("ck_bids_status", "bids", type_="check")
    op.create_check_constraint(
        "ck_bids_status",
        "bids",
        "status IN ('active','outbid','cancelled','won','lost','forfeited')",
    )

    # --- holds: what the capture actually cost ---------------------------------------------
    # Read from the charge's balance_transaction at capture. The ledger's stripe_fees leg
    # needs the real number, and the order created later carries it forward so one
    # `order_paid` transaction can account for both charges.
    op.add_column(
        "holds",
        sa.Column("capture_fee_cents", sa.Integer(), server_default="0", nullable=False),
    )

    # --- orders: money collected before checkout -------------------------------------------
    # Nonzero only for an auction win, where the hold capture at close already took the
    # hammer price. The payment intent at checkout is priced at total - prepaid, so the
    # buyer is charged the remainder and never the artwork twice.
    op.add_column(
        "orders", sa.Column("prepaid_cents", sa.Integer(), server_default="0", nullable=False)
    )
    # Stripe's fee on that earlier capture. The ledger books one `order_paid` transaction for
    # the whole order, and its stripe_fees leg has to account for both charges or
    # platform_clearing drifts from the real balance by the difference.
    op.add_column(
        "orders",
        sa.Column("prepaid_fee_cents", sa.Integer(), server_default="0", nullable=False),
    )
    # The hold's PaymentIntent. Refunding an auction order has to reverse this one too.
    op.add_column("orders", sa.Column("prepaid_reference", sa.String(255), nullable=True))
    op.create_check_constraint(
        "ck_orders_prepaid_within_total",
        "orders",
        "prepaid_cents >= 0 AND prepaid_cents <= total_cents",
    )


    # --- ledger: somewhere for captured hammer money to live --------------------------------
    # A close captures the winner's hold before any order exists, so until now that money had
    # nowhere to be booked. Leaving it unbooked was not an option: a winner who never
    # completes checkout would leave real money in Stripe with no ledger record at all.
    #
    # A liability, not income: the platform is holding it on behalf of a sale that has not
    # completed, and it is either cleared into an order or refunded.
    ledger_accounts = sa.table(
        "ledger_accounts",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("type", sa.String),
        sa.column("owner_id", postgresql.UUID(as_uuid=True)),
        sa.column("currency", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        ledger_accounts,
        [{
            "id": uuid.uuid4(),
            "type": "auction_escrow",
            "owner_id": None,
            "currency": "USD",
            "created_at": datetime.now(timezone.utc),
        }],
    )


def downgrade() -> None:
    op.execute("DELETE FROM ledger_accounts WHERE type = 'auction_escrow'")

    op.drop_constraint("ck_orders_prepaid_within_total", "orders", type_="check")
    op.drop_column("orders", "prepaid_reference")
    op.drop_column("orders", "prepaid_fee_cents")
    op.drop_column("orders", "prepaid_cents")

    op.drop_column("holds", "capture_fee_cents")

    op.drop_constraint("ck_bids_status", "bids", type_="check")
    op.create_check_constraint(
        "ck_bids_status", "bids", "status IN ('active','outbid','cancelled','won','lost')"
    )

    op.drop_constraint("ck_auctions_cascade_depth_positive", "auctions", type_="check")
    op.drop_constraint("ck_auctions_status", "auctions", type_="check")
    op.create_check_constraint(
        "ck_auctions_status",
        "auctions",
        "status IN ('draft','live','closing','closed_sold','closed_reserve_not_met',"
        "'closed_no_bids','needs_seller_action','cancelled')",
    )

    op.drop_index("ix_auctions_awaiting_winner_deadline", table_name="auctions")
    op.drop_column("auctions", "cascade_depth")
    op.drop_column("auctions", "winner_deadline_at")
    op.drop_constraint("fk_auctions_winning_bid_id", "auctions", type_="foreignkey")
    op.drop_column("auctions", "winning_bid_id")
