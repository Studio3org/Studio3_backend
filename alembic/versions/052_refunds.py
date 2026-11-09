"""Refunds: one row per Stripe refund the platform issues.

Revision ID: 052_refunds
Revises: 051_profile_extras

Every refund path (admin dispute refund, artist cancelling a paid order, a payment that
landed after its order was cancelled) goes through one service, which records the refund
here before calling Stripe. The row is what makes a retry safe and a failed refund visible:
before this, a refund that failed at Stripe left no trace except a log line.

`payment_intent_id` is unique — there is only ever one full refund of a given payment, so it
doubles as the duplicate guard. No existing rows: refunds issued before this were booked in
the ledger only.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "052_refunds"
down_revision: Union[str, None] = "051_profile_extras"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "refunds",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column("payment_intent_id", sa.String(255), nullable=False, unique=True),
        sa.Column("leg", sa.String(16), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("stripe_refund_id", sa.String(255), nullable=True),
        sa.Column(
            "initiated_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("initiated_by_role", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("leg IN ('charge', 'prepaid', 'late')", name="ck_refunds_leg"),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed')", name="ck_refunds_status"
        ),
        sa.CheckConstraint("amount_cents >= 0", name="ck_refunds_amount"),
    )
    op.create_index("ix_refunds_status", "refunds", ["status"])


def downgrade() -> None:
    op.drop_index("ix_refunds_status", table_name="refunds")
    op.drop_table("refunds")
