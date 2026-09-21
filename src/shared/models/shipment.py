"""Shipment — manually entered courier booking and tracking (Phase 1: no carrier API).

Ops books FedEx/UPS by hand and records the result here; there is no delivery webhook, so
`delivered` is also set manually, which is what moves the order to awaiting_confirmation.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Integer, ForeignKey
from sqlalchemy.dialects.postgresql import UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


SHIPMENT_LABEL_CREATED = "label_created"
SHIPMENT_PICKED_UP = "picked_up"
SHIPMENT_IN_TRANSIT = "in_transit"
SHIPMENT_DELIVERED = "delivered"


class Shipment(Base):
    __tablename__ = "shipments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(
        UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    courier = Column(String(64), nullable=False)
    tracking_number = Column(String(128), nullable=False)
    shipment_date = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(24), default=SHIPMENT_LABEL_CREATED, nullable=False)
    # What the courier actually charged the platform, entered by ops. The collector pays a
    # flat shipping rate that rarely matches this for crated art, so without recording it
    # the platform's shipping margin (often negative) is invisible. Booked to the ledger as
    # a shipping_costs expense against platform_bank — the courier is paid outside Stripe.
    actual_shipping_cost_cents = Column(Integer, nullable=True)
    created_by_admin_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
