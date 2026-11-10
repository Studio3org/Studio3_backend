"""Phase 8: Convelio shipping, checkout pricing and delivery tracking (Convelio plan; B19,
B31, B32).

Revision ID: 060_convelio_shipping
Revises: 059_auctions_v2

* shipping_quotes: a Convelio quote for one piece to one address — speed, delivery type,
  insurance, the three price amounts (excl. VAT, VAT, incl. VAT) and insurance, and whether
  it is instant (`created`) or a custom quote still being priced (`processing`).
* orders: the quote it was bought on, the delivery type, insurance and the tax calculation.
* shipments: Convelio's order id and reference, its raw status, the tracking and dashboard
  links, the booking state (we can't ask Convelio whether an order exists, so a booking whose
  outcome is unknown is never retried blind — D-6) and when it was delivered (auto-complete).
  Courier and tracking number become optional (Convelio provides a link instead).
* shipment_documents: documents Convelio says are ready (proof of delivery and so on).
* provider_webhook_events: every Convelio delivery, stored before it is processed — Convelio
  never redelivers after an error (D-7). Deduplicated by a hash of the body.
* users.pickup_address_id: the artist's studio pickup address (B31), private to them.
* pieces: Convelio's item type, packing and materials, beside the existing package size,
  weight and declared value.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "060_convelio_shipping"
down_revision: Union[str, None] = "059_auctions_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def _stamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def upgrade() -> None:
    op.create_table(
        "shipping_quotes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("piece_id", UUID, sa.ForeignKey("pieces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("buyer_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("address_id", UUID, sa.ForeignKey("addresses.id", ondelete="SET NULL"), nullable=True),
        sa.Column("provider", sa.String(16), nullable=False, server_default="convelio"),
        sa.Column("provider_quote_id", sa.String(64), nullable=True, unique=True),
        sa.Column("provider_ref", sa.String(64), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="processing"),
        sa.Column("shipping_speed", sa.String(16), nullable=False),
        sa.Column("delivery_type", sa.String(32), nullable=False),
        sa.Column("insurance", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("price_excl_cents", sa.Integer, nullable=True),
        sa.Column("vat_cents", sa.Integer, nullable=True),
        sa.Column("price_incl_cents", sa.Integer, nullable=True),
        sa.Column("insurance_cents", sa.Integer, nullable=True),
        sa.Column("request_snapshot", postgresql.JSONB, nullable=True),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        *_stamps(),
        sa.CheckConstraint("status IN ('created','processing','ready','expired','used','failed')",
                           name="ck_shipping_quotes_status"),
    )
    op.create_index("ix_shipping_quotes_buyer_piece", "shipping_quotes", ["buyer_id", "piece_id"])

    op.add_column("orders", sa.Column("shipping_quote_id", UUID,
                                      sa.ForeignKey("shipping_quotes.id", ondelete="SET NULL"), nullable=True))
    op.add_column("orders", sa.Column("delivery_type", sa.String(32), nullable=True))
    op.add_column("orders", sa.Column("insurance_cents", sa.Integer, nullable=False, server_default="0"))
    op.add_column("orders", sa.Column("tax_calculation_id", sa.String(255), nullable=True))

    op.alter_column("shipments", "courier", existing_type=sa.String(64), nullable=True)
    op.alter_column("shipments", "tracking_number", existing_type=sa.String(128), nullable=True)
    for column in (
        sa.Column("provider", sa.String(16), nullable=True),
        sa.Column("provider_order_id", sa.String(64), nullable=True, unique=True),
        sa.Column("provider_order_ref", sa.String(64), nullable=True),
        sa.Column("provider_status", sa.String(32), nullable=True),
        sa.Column("tracking_link", sa.String(1024), nullable=True),
        sa.Column("dashboard_link", sa.String(1024), nullable=True),
        sa.Column("booking_state", sa.String(12), nullable=True),
        sa.Column("booking_error", sa.Text, nullable=True),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("shipments", column)

    op.create_table(
        "shipment_documents",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("shipment_id", UUID, sa.ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("document_type", sa.String(64), nullable=False),
        sa.Column("dashboard_link", sa.String(1024), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("shipment_id", "document_type", name="uq_shipment_document"),
    )

    op.create_table(
        "provider_webhook_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("body_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_provider_webhook_events_type", "provider_webhook_events", ["provider", "event_type"])

    op.add_column("users", sa.Column("pickup_address_id", UUID,
                                     sa.ForeignKey("addresses.id", ondelete="SET NULL"), nullable=True))
    op.add_column("pieces", sa.Column("shipping_item_type", sa.String(32), nullable=True))
    op.add_column("pieces", sa.Column("shipping_packing", sa.String(16), nullable=True))
    op.add_column("pieces", sa.Column("shipping_materials", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    for column in ("shipping_materials", "shipping_packing", "shipping_item_type"):
        op.drop_column("pieces", column)
    op.drop_column("users", "pickup_address_id")
    op.drop_index("ix_provider_webhook_events_type", table_name="provider_webhook_events")
    op.drop_table("provider_webhook_events")
    op.drop_table("shipment_documents")
    for column in ("delivered_at", "last_event_at", "booking_error", "booking_state", "dashboard_link",
                   "tracking_link", "provider_status", "provider_order_ref", "provider_order_id", "provider"):
        op.drop_column("shipments", column)
    # Provider shipments have no courier or tracking number; fill them so the old NOT NULLs hold.
    op.execute("UPDATE shipments SET courier = 'convelio' WHERE courier IS NULL")
    op.execute("UPDATE shipments SET tracking_number = '-' WHERE tracking_number IS NULL")
    op.alter_column("shipments", "tracking_number", existing_type=sa.String(128), nullable=False)
    op.alter_column("shipments", "courier", existing_type=sa.String(64), nullable=False)
    for column in ("tax_calculation_id", "insurance_cents", "delivery_type", "shipping_quote_id"):
        op.drop_column("orders", column)
    op.drop_index("ix_shipping_quotes_buyer_piece", table_name="shipping_quotes")
    op.drop_table("shipping_quotes")
