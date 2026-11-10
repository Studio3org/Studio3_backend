"""Phase 7b: shipping, insurance and tax charged automatically after an auction win.

Revision ID: 061_auction_auto_checkout
Revises: 060_convelio_shipping

The 9 Nov design: no checkout after winning. Once the hammer price is charged, the winner's
Ship to address is quoted with Convelio and the rest is charged to the same saved card.

* auctions.auto_checkout_state: `pending_quote` (waiting for Convelio's custom quote),
  `charged`, `failed` (the winner finishes at the auction checkout instead) or `manual`
  (pickup, no address, Convelio off: the auction checkout as before).
* auctions.shipping_quote_id: the quote it waits on. auctions.checkout_order_id: the order.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "061_auction_auto_checkout"
down_revision: Union[str, None] = "060_convelio_shipping"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column("auctions", sa.Column("auto_checkout_state", sa.String(16), nullable=True))
    op.add_column("auctions", sa.Column("shipping_quote_id", UUID,
                                        sa.ForeignKey("shipping_quotes.id", ondelete="SET NULL"), nullable=True))
    op.add_column("auctions", sa.Column("checkout_order_id", UUID,
                                        sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True))
    op.create_check_constraint(
        "ck_auctions_auto_checkout_state", "auctions",
        "auto_checkout_state IS NULL OR auto_checkout_state IN ('pending_quote','charged','failed','manual')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_auctions_auto_checkout_state", "auctions", type_="check")
    op.drop_column("auctions", "checkout_order_id")
    op.drop_column("auctions", "shipping_quote_id")
    op.drop_column("auctions", "auto_checkout_state")
