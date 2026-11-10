"""Refund — one Stripe refund the platform issued, recorded before Stripe is called.

The ledger says what a refund did to the books; this row says whether the money actually
went back. A refund that failed at Stripe stays here as `failed` for an admin to retry, and
the order's payout stays blocked until it does (refunds_service).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


REFUND_PENDING = "pending"
REFUND_SUCCEEDED = "succeeded"
REFUND_FAILED = "failed"

# Which payment the refund returns. An auction order is paid twice: the hammer price
# captured when the auction closed (`prepaid`) and the balance at checkout (`charge`).
# `late` is a payment that succeeded after its order was already cancelled.
LEG_CHARGE = "charge"
LEG_PREPAID = "prepaid"
LEG_LATE = "late"

# Who started it. `system` is the webhook refunding a late payment.
ROLE_ADMIN = "admin"
ROLE_SELLER = "seller"
ROLE_SYSTEM = "system"


class Refund(Base):
    __tablename__ = "refunds"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # A ticket order's refund carries this instead of order_id (event tickets, Phase 3+).
    ticket_order_id = Column(
        UUID(as_uuid=True), ForeignKey("event_ticket_orders.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    # One full refund per payment, ever — so this is also the duplicate guard.
    payment_intent_id = Column(String(255), nullable=False, unique=True)
    leg = Column(String(16), nullable=False)
    amount_cents = Column(Integer, nullable=False)
    status = Column(String(16), default=REFUND_PENDING, nullable=False)
    stripe_refund_id = Column(String(255), nullable=True)
    initiated_by_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    initiated_by_role = Column(String(16), nullable=False)
    reason = Column(Text, nullable=True)
    failure_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
