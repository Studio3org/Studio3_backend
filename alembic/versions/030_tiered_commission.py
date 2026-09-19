"""Per-sale commission snapshot + per-seller rate override.

Revision ID: 030_tiered_commission
Revises: 029_piece_listing_integrity
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "030_tiered_commission"
down_revision: Union[str, None] = "029_piece_listing_integrity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# What the platform charged before tiered rates existed. Historical orders were created
# under this rate and must keep it: backfilling today's rate would restate what past
# artists are owed, which is the exact failure the snapshot column prevents.
LEGACY_COMMISSION_BPS = 1000


def upgrade() -> None:
    # The rate this specific order was sold at. Refunds and payouts read it instead of
    # recomputing from config, so changing the platform rate never rewrites history.
    op.add_column("orders", sa.Column("commission_bps", sa.Integer(), nullable=True))
    op.execute(f"UPDATE orders SET commission_bps = {LEGACY_COMMISSION_BPS} "
               "WHERE commission_bps IS NULL")
    op.alter_column("orders", "commission_bps", nullable=False)
    op.create_check_constraint(
        "ck_orders_commission_bps_range", "orders", "commission_bps BETWEEN 0 AND 10000"
    )

    # The client's reduced tier for a named set of artists. Null means the standard rate —
    # a nullable override rather than a populated default, so "this artist is on a special
    # deal" is distinguishable from "this artist happens to match the current default".
    op.add_column("users", sa.Column("commission_bps_override", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_users_commission_bps_override_range",
        "users",
        "commission_bps_override IS NULL OR commission_bps_override BETWEEN 0 AND 10000",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_commission_bps_override_range", "users", type_="check")
    op.drop_column("users", "commission_bps_override")
    op.drop_constraint("ck_orders_commission_bps_range", "orders", type_="check")
    op.drop_column("orders", "commission_bps")
