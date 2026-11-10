"""Event ticketing: ticket types, orders and tickets; refund policy, age limit, visibility.

Revision ID: 054_event_ticketing
Revises: 053_commission_override_until

BACKEND_CHANGES B22 (and B28's age limit and visibility), built to the ticketing spec of
25 Oct 2026.

* event_ticket_types: what a host sells (price, or 0 for free; stock; per-order limit; sale
  window). A type that has sold is closed, never deleted.
* event_ticket_orders + event_ticket_order_items: one checkout, several types. Items are
  rows rather than JSON so stock can be counted in SQL under the ticket-type row lock.
  `hold_expires_at` is the 10-minute checkout hold.
* event_tickets: one admission each, with a short public number. The QR value is not stored:
  it is a signature over the ticket id, made on demand (ticket_codes.py).
* events: refund_policy (7d / 24h / none), age_limit (all / 18 / 21), visibility
  (public / link). Existing events: no refunds, all ages, public — what they were.
* refunds.ticket_order_id: a refund can belong to a ticket order instead of an art order.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "054_event_ticketing"
down_revision: Union[str, None] = "053_commission_override_until"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.add_column("events", sa.Column(
        "refund_policy", sa.String(8), nullable=False, server_default="none"))
    op.add_column("events", sa.Column(
        "age_limit", sa.String(4), nullable=False, server_default="all"))
    op.add_column("events", sa.Column(
        "visibility", sa.String(8), nullable=False, server_default="public"))
    op.create_check_constraint(
        "ck_events_refund_policy", "events", "refund_policy IN ('7d', '24h', 'none')")
    op.create_check_constraint("ck_events_age_limit", "events", "age_limit IN ('all', '18', '21')")
    op.create_check_constraint("ck_events_visibility", "events", "visibility IN ('public', 'link')")

    op.create_table(
        "event_ticket_types",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("event_id", UUID, sa.ForeignKey("events.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("includes", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("max_per_order", sa.Integer(), nullable=True),
        sa.Column("sale_starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sale_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("price_cents >= 0", name="ck_ticket_types_price"),
        sa.CheckConstraint("quantity IS NULL OR quantity >= 0", name="ck_ticket_types_quantity"),
        sa.CheckConstraint("max_per_order IS NULL OR max_per_order > 0",
                           name="ck_ticket_types_max_per_order"),
    )

    op.create_table(
        "event_ticket_orders",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("event_id", UUID, sa.ForeignKey("events.id", ondelete="RESTRICT"),
                  nullable=False, index=True),
        sa.Column("buyer_id", UUID, sa.ForeignKey("users.id", ondelete="RESTRICT"),
                  nullable=False, index=True),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("subtotal_cents", sa.Integer(), nullable=False),
        sa.Column("tax_cents", sa.Integer(), nullable=False),
        sa.Column("total_cents", sa.Integer(), nullable=False),
        sa.Column("fee_bps", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("payment_reference", sa.String(255), nullable=True, unique=True),
        sa.Column("stripe_charge_id", sa.String(255), nullable=True),
        sa.Column("hold_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("kind IN ('purchase', 'rsvp')", name="ck_ticket_orders_kind"),
        sa.CheckConstraint(
            "status IN ('pending_payment', 'paid', 'expired', 'cancelled', 'failed', "
            "'refunded', 'partly_refunded')",
            name="ck_ticket_orders_status",
        ),
        sa.CheckConstraint("total_cents >= 0", name="ck_ticket_orders_total"),
    )
    op.create_index("ix_ticket_orders_status_hold", "event_ticket_orders",
                    ["status", "hold_expires_at"])

    op.create_table(
        "event_ticket_order_items",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("order_id", UUID, sa.ForeignKey("event_ticket_orders.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("ticket_type_id", UUID,
                  sa.ForeignKey("event_ticket_types.id", ondelete="RESTRICT"),
                  nullable=True, index=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_cents", sa.Integer(), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_ticket_order_items_quantity"),
    )

    op.create_table(
        "event_tickets",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("order_id", UUID, sa.ForeignKey("event_ticket_orders.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("event_id", UUID, sa.ForeignKey("events.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("ticket_type_id", UUID,
                  sa.ForeignKey("event_ticket_types.id", ondelete="RESTRICT"),
                  nullable=True, index=True),
        sa.Column("holder_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("number", sa.String(16), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_in_by_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('valid', 'checked_in', 'refunded', 'void')",
                           name="ck_event_tickets_status"),
    )

    op.add_column("refunds", sa.Column(
        "ticket_order_id", UUID,
        sa.ForeignKey("event_ticket_orders.id", ondelete="CASCADE"), nullable=True))
    op.create_index("ix_refunds_ticket_order_id", "refunds", ["ticket_order_id"])


def downgrade() -> None:
    op.drop_index("ix_refunds_ticket_order_id", table_name="refunds")
    op.drop_column("refunds", "ticket_order_id")
    op.drop_table("event_tickets")
    op.drop_table("event_ticket_order_items")
    op.drop_index("ix_ticket_orders_status_hold", table_name="event_ticket_orders")
    op.drop_table("event_ticket_orders")
    op.drop_table("event_ticket_types")
    op.drop_constraint("ck_events_visibility", "events", type_="check")
    op.drop_constraint("ck_events_age_limit", "events", type_="check")
    op.drop_constraint("ck_events_refund_policy", "events", type_="check")
    op.drop_column("events", "visibility")
    op.drop_column("events", "age_limit")
    op.drop_column("events", "refund_policy")
