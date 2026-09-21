"""Marketplace Phase 1: admin role, Connect fields, shipping attributes, ledger, payouts,
shipments, disputes, webhook audit log.

All Phase 1 schema lands in this one migration so later work adds behavior, not migrations.

Revision ID: 022_marketplace_payouts_schema
Revises: 021_social_engagement_indexes
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "022_marketplace_payouts_schema"
down_revision: Union[str, None] = "021_social_engagement_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Platform-level ledger accounts (owner_id NULL). Seeded below so the first payment never
# races to create them. Per-seller `seller_payable` accounts are created lazily at runtime.
PLATFORM_ACCOUNT_TYPES = (
    "platform_clearing",   # asset: money in the Stripe balance
    "platform_bank",       # asset: platform's own bank, outside Stripe
    "platform_revenue",    # income: commission + collected shipping
    "stripe_fees",         # expense: Stripe processing fees absorbed by the platform
    "shipping_costs",      # expense: what the courier actually charged
    "tax_payable",         # liability: tax collected, owed onward
)


def upgrade() -> None:
    # --- users: admin flag + Stripe Connect ---
    op.add_column("users", sa.Column("is_admin", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("users", sa.Column("stripe_account_id", sa.String(255), nullable=True))
    op.add_column(
        "users",
        sa.Column("stripe_payouts_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
    )

    # --- pieces: courier-facing shipping attributes ---
    op.add_column("pieces", sa.Column("weight_kg", sa.Float(), nullable=True))
    op.add_column("pieces", sa.Column("package_length_cm", sa.Float(), nullable=True))
    op.add_column("pieces", sa.Column("package_width_cm", sa.Float(), nullable=True))
    op.add_column("pieces", sa.Column("package_height_cm", sa.Float(), nullable=True))
    op.add_column("pieces", sa.Column("declared_value_cents", sa.Integer(), nullable=True))

    # --- orders: delivery confirmation + charge reference ---
    op.add_column("orders", sa.Column("stripe_charge_id", sa.String(255), nullable=True))
    op.add_column("orders", sa.Column("received", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("orders", sa.Column("received_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f("ix_orders_payment_reference"), "orders", ["payment_reference"], unique=False)

    # --- ledger ---
    ledger_accounts = op.create_table(
        "ledger_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("currency", sa.String(3), server_default="USD", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("type", "owner_id", name="uq_ledger_accounts_type_owner"),
    )
    op.create_index(op.f("ix_ledger_accounts_type"), "ledger_accounts", ["type"], unique=False)
    op.create_index(op.f("ix_ledger_accounts_owner_id"), "ledger_accounts", ["owner_id"], unique=False)
    # Postgres treats NULLs as distinct in a UNIQUE constraint, so (type, NULL) would not
    # stop duplicate platform singletons being inserted. This partial index does.
    op.create_index(
        "uq_ledger_accounts_platform_singleton",
        "ledger_accounts",
        ["type"],
        unique=True,
        postgresql_where=sa.text("owner_id IS NULL"),
    )

    op.create_table(
        "ledger_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_ledger_transactions_idempotency_key"),
    )
    op.create_index(op.f("ix_ledger_transactions_order_id"), "ledger_transactions", ["order_id"], unique=False)
    op.create_index(op.f("ix_ledger_transactions_type"), "ledger_transactions", ["type"], unique=False)

    op.create_table(
        "ledger_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("direction", sa.String(6), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["transaction_id"], ["ledger_transactions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["account_id"], ["ledger_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("amount_cents > 0", name="ck_ledger_entries_amount_positive"),
        sa.CheckConstraint("direction IN ('debit', 'credit')", name="ck_ledger_entries_direction"),
    )
    op.create_index(op.f("ix_ledger_entries_transaction_id"), "ledger_entries", ["transaction_id"], unique=False)
    op.create_index(op.f("ix_ledger_entries_account_id"), "ledger_entries", ["account_id"], unique=False)

    now = datetime.now(timezone.utc)
    op.bulk_insert(
        ledger_accounts,
        [
            {"id": uuid.uuid4(), "type": t, "owner_id": None, "currency": "USD", "created_at": now}
            for t in PLATFORM_ACCOUNT_TYPES
        ],
    )

    # --- payouts ---
    op.create_table(
        "payouts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seller_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ledger_transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(24), server_default="pending", nullable=False),
        sa.Column("stripe_transfer_id", sa.String(255), nullable=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["seller_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ledger_transaction_id"], ["ledger_transactions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        # One payout per order — this constraint is itself a duplicate-payout guard.
        sa.UniqueConstraint("order_id", name="uq_payouts_order_id"),
        sa.UniqueConstraint("stripe_transfer_id", name="uq_payouts_stripe_transfer_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_payouts_idempotency_key"),
    )
    op.create_index(op.f("ix_payouts_order_id"), "payouts", ["order_id"], unique=False)
    op.create_index(op.f("ix_payouts_seller_id"), "payouts", ["seller_id"], unique=False)
    op.create_index(op.f("ix_payouts_status"), "payouts", ["status"], unique=False)

    # --- shipments ---
    op.create_table(
        "shipments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("courier", sa.String(64), nullable=False),
        sa.Column("tracking_number", sa.String(128), nullable=False),
        sa.Column("shipment_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(24), server_default="label_created", nullable=False),
        sa.Column("actual_shipping_cost_cents", sa.Integer(), nullable=True),
        sa.Column("created_by_admin_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_admin_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", name="uq_shipments_order_id"),
    )
    op.create_index(op.f("ix_shipments_order_id"), "shipments", ["order_id"], unique=False)

    # --- disputes ---
    op.create_table(
        "disputes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("opened_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), server_default="open", nullable=False),
        sa.Column("resolved_by_admin_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opened_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["resolved_by_admin_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id", name="uq_disputes_order_id"),
    )
    op.create_index(op.f("ix_disputes_order_id"), "disputes", ["order_id"], unique=False)
    op.create_index(op.f("ix_disputes_status"), "disputes", ["status"], unique=False)

    # --- stripe webhook audit log / idempotency guard ---
    op.create_table(
        "stripe_webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stripe_event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stripe_event_id", name="uq_stripe_webhook_events_event_id"),
    )
    op.create_index(
        op.f("ix_stripe_webhook_events_stripe_event_id"),
        "stripe_webhook_events",
        ["stripe_event_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_stripe_webhook_events_event_type"), "stripe_webhook_events", ["event_type"], unique=False
    )


def downgrade() -> None:
    op.drop_table("stripe_webhook_events")
    op.drop_table("disputes")
    op.drop_table("shipments")
    op.drop_table("payouts")
    op.drop_table("ledger_entries")
    op.drop_table("ledger_transactions")
    op.drop_table("ledger_accounts")

    op.drop_index(op.f("ix_orders_payment_reference"), table_name="orders")
    op.drop_column("orders", "received_at")
    op.drop_column("orders", "received")
    op.drop_column("orders", "stripe_charge_id")

    op.drop_column("pieces", "declared_value_cents")
    op.drop_column("pieces", "package_height_cm")
    op.drop_column("pieces", "package_width_cm")
    op.drop_column("pieces", "package_length_cm")
    op.drop_column("pieces", "weight_kg")

    op.drop_column("users", "stripe_payouts_enabled")
    op.drop_column("users", "stripe_account_id")
    op.drop_column("users", "is_admin")
