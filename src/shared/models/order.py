"""Order / OrderItem — Stripe-backed checkout with payout held until delivery confirmation.

Money is captured to the platform's Stripe balance up front and only transferred to the
artist once the collector confirms receipt (order reaches `completed`). All money-affecting
status changes are driven by verified Stripe webhooks, never client input — see
src/modules/payments/webhook_handler.py.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(
            "commission_bps BETWEEN 0 AND 10000", name="ck_orders_commission_bps_range"
        ),
        CheckConstraint(
            "prepaid_cents >= 0 AND prepaid_cents <= total_cents",
            name="ck_orders_prepaid_within_total",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    buyer_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Denormalized; v1 checkout is one piece at a time so an order always has a single seller.
    seller_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # pending_payment|paid|shipped|awaiting_confirmation|completed|cancelled|failed|refunded|disputed
    # Transitions are enforced in one place: orders_dao.transition_order().
    status = Column(String(24), default="pending_payment", nullable=False)
    shipping_method = Column(String(16), nullable=False)  # standard|express|overnight|free
    # Denormalized copy of the chosen Address's fields at order time — NOT a FK, so
    # later edits to the saved address never change historical orders.
    shipping_address_snapshot = Column(JSONB, nullable=False)
    artwork_cents = Column(Integer, nullable=False)
    shipping_cents = Column(Integer, nullable=False)
    tax_cents = Column(Integer, nullable=False)
    total_cents = Column(Integer, nullable=False)
    # The commission rate this order was sold at, resolved once at checkout. Refunds
    # and payouts read this rather than today's configured rate, so changing the rate
    # never restates what a past artist is owed.
    commission_bps = Column(Integer, nullable=False)
    # Money already collected before this order existed. Nonzero only for an auction win,
    # where capturing the winner's hold at close already took the hammer price — the payment
    # intent at checkout is priced at total - prepaid so the artwork is not charged twice.
    prepaid_cents = Column(Integer, default=0, server_default="0", nullable=False)
    # Stripe's fee on that earlier capture. One `order_paid` ledger transaction covers the
    # whole order, so its stripe_fees leg has to account for both charges or platform_clearing
    # drifts from the real Stripe balance by the difference.
    prepaid_fee_cents = Column(Integer, default=0, server_default="0", nullable=False)
    # The hold's PaymentIntent. A refund on an auction order has to reverse this one too.
    prepaid_reference = Column(String(255), nullable=True)
    payment_provider = Column(String(32), nullable=True)  # null while unconfigured; else "stripe"
    payment_reference = Column(String(255), nullable=True, index=True)  # Stripe PaymentIntent id
    # Charge id behind the PaymentIntent. Needed for refunds and as `source_transaction` on
    # the payout Transfer, which lets Stripe release funds as the charge settles instead of
    # failing with balance_insufficient.
    stripe_charge_id = Column(String(255), nullable=True)
    # Collector's delivery confirmation — the trigger for artist payout release.
    received = Column(Boolean, default=False, nullable=False)
    received_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    # RESTRICT (not CASCADE like elsewhere): pieces are only ever soft-deleted
    # (deleted_at) in this codebase, never hard-deleted, so this is defensive, not
    # something any current code path exercises.
    piece_id = Column(UUID(as_uuid=True), ForeignKey("pieces.id", ondelete="RESTRICT"), nullable=False, index=True)
    price_cents = Column(Integer, nullable=False)  # snapshot at order time
    quantity = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
