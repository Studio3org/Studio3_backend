"""Founding-artist rate: an end date on the per-seller commission override.

Revision ID: 053_commission_override_until
Revises: 052_refunds

BACKEND_CHANGES B3. The founding-artist tier is "15% for their first 12 months", but the
override had no end, so it applied forever. users.commission_override_until (null = no end,
which is how every existing override is left) — after it, resolve_bps falls back to the
standard rate. Rates already snapshotted on orders and auctions are unaffected.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "053_commission_override_until"
down_revision: Union[str, None] = "052_refunds"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("commission_override_until", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("users", "commission_override_until")
