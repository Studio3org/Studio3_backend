"""Shipping through Convelio (Phase 8): quotes, documents and the webhook inbox.

* ShippingQuote — one Convelio quote for one piece to one buyer address. `created` is an
  instant price; `processing` is a custom quote Convelio prices within 24 hours (it becomes
  `ready` on their `custom_quote_ready` webhook). The order pays the price stored here; the
  app never sends a price.
* ShipmentDocument — a document Convelio says is ready (proof of delivery, invoice).
* ProviderWebhookEvent — every Convelio delivery, stored before it is processed: Convelio
  never redelivers after an error, so nothing may fail before the row exists (D-7).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


QUOTE_CREATED = "created"        # an instant price, ready to buy
QUOTE_PROCESSING = "processing"  # a custom quote; Convelio prices it within 24 hours
QUOTE_READY = "ready"            # a custom quote that has been priced
QUOTE_EXPIRED = "expired"
QUOTE_USED = "used"              # an order has been placed on it
QUOTE_FAILED = "failed"
BUYABLE_QUOTE_STATUSES = (QUOTE_CREATED, QUOTE_READY)

SPEED_REGULAR = "regular_speed"
SPEED_EXPRESS = "express"
SHIPPING_SPEEDS = (SPEED_REGULAR, SPEED_EXPRESS)
DELIVERY_CURBSIDE = "curbside"
DELIVERY_WHITE_GLOVE = "white_glove"
DELIVERY_TYPES = (DELIVERY_CURBSIDE, DELIVERY_WHITE_GLOVE)

BOOKING_BOOKING = "booking"   # we're about to call Convelio (or did, and haven't heard)
BOOKING_BOOKED = "booked"
BOOKING_UNKNOWN = "unknown"   # the call timed out: never retried blind (D-6)
BOOKING_FAILED = "failed"     # Convelio said no; ops can retry from the admin console
BOOKING_CANCELED = "canceled"


class ShippingQuote(Base):
    __tablename__ = "shipping_quotes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    piece_id = Column(UUID(as_uuid=True), ForeignKey("pieces.id", ondelete="CASCADE"), nullable=False)
    buyer_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    address_id = Column(UUID(as_uuid=True), ForeignKey("addresses.id", ondelete="SET NULL"), nullable=True)
    provider = Column(String(16), nullable=False, default="convelio")
    provider_quote_id = Column(String(64), nullable=True, unique=True)
    provider_ref = Column(String(64), nullable=True)
    status = Column(String(12), nullable=False, default=QUOTE_PROCESSING)
    shipping_speed = Column(String(16), nullable=False)
    delivery_type = Column(String(32), nullable=False)
    insurance = Column(Boolean, nullable=False, default=True)
    currency = Column(String(3), nullable=False, default="USD")
    price_excl_cents = Column(Integer, nullable=True)
    vat_cents = Column(Integer, nullable=True)
    price_incl_cents = Column(Integer, nullable=True)
    insurance_cents = Column(Integer, nullable=True)
    request_snapshot = Column(JSONB, nullable=True)
    ready_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    @property
    def shipping_cents(self) -> int:
        """What the buyer pays for the shipping itself (VAT-inclusive; zero VAT in the US)."""
        return int(self.price_incl_cents or 0)

    @property
    def total_cents(self) -> int:
        return self.shipping_cents + (int(self.insurance_cents or 0) if self.insurance else 0)


class ShipmentDocument(Base):
    __tablename__ = "shipment_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shipment_id = Column(UUID(as_uuid=True), ForeignKey("shipments.id", ondelete="CASCADE"), nullable=False)
    document_type = Column(String(64), nullable=False)
    dashboard_link = Column(String(1024), nullable=True)
    received_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class ProviderWebhookEvent(Base):
    __tablename__ = "provider_webhook_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider = Column(String(16), nullable=False)
    body_hash = Column(String(64), nullable=False, unique=True)
    event_type = Column(String(64), nullable=False)
    payload = Column(JSONB, nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
