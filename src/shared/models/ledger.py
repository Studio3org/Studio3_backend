"""Double-entry ledger for marketplace money movement.

Every cents-moving event is one balanced LedgerTransaction: its debit entries must sum to
exactly its credit entries. Rows are append-only — corrections are new reversing
transactions, never updates or deletes — so the ledger doubles as the money audit trail.

Adding a new charge type later (insurance, framing surcharge, listing fee) means a new
account type and one more entry line, not a schema change.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    String,
    Integer,
    Text,
    ForeignKey,
    CheckConstraint,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


# Account types. Platform accounts are singletons (owner_id NULL); seller_payable has one
# row per artist (owner_id = users.id).
ACCOUNT_PLATFORM_CLEARING = "platform_clearing"  # asset: money sitting in the Stripe balance
ACCOUNT_PLATFORM_BANK = "platform_bank"          # asset: platform's own bank, outside Stripe
ACCOUNT_PLATFORM_REVENUE = "platform_revenue"    # income: commission + collected shipping
ACCOUNT_STRIPE_FEES = "stripe_fees"              # expense: Stripe processing fees absorbed
ACCOUNT_SHIPPING_COSTS = "shipping_costs"        # expense: what the courier actually charged
ACCOUNT_TAX_PAYABLE = "tax_payable"              # liability: tax collected, owed onward
ACCOUNT_SELLER_PAYABLE = "seller_payable"        # liability: owed to a specific artist
# liability: hammer price captured from an auction winner before any order exists. Cleared
# into the order at checkout, or refunded if the winner forfeits. Without it, the money a
# close captures would sit in Stripe with no ledger record until — or unless — the winner
# completed checkout.
ACCOUNT_AUCTION_ESCROW = "auction_escrow"

PLATFORM_ACCOUNT_TYPES = (
    ACCOUNT_PLATFORM_CLEARING,
    ACCOUNT_PLATFORM_BANK,
    ACCOUNT_PLATFORM_REVENUE,
    ACCOUNT_STRIPE_FEES,
    ACCOUNT_SHIPPING_COSTS,
    ACCOUNT_TAX_PAYABLE,
    ACCOUNT_AUCTION_ESCROW,
)

# Transaction types.
TXN_ORDER_PAID = "order_paid"
TXN_PAYOUT_RELEASED = "payout_released"
TXN_REFUND_ISSUED = "refund_issued"
TXN_SHIPPING_COST_RECORDED = "shipping_cost_recorded"
TXN_TRANSFER_REVERSED = "transfer_reversed"
# An auction close capturing the winner's hold, before there is an order to attach it to.
TXN_AUCTION_CAPTURED = "auction_captured"
# That capture given back when the winner forfeits or the sale cannot complete.
TXN_AUCTION_REFUNDED = "auction_refunded"

DEBIT = "debit"
CREDIT = "credit"


class LedgerAccount(Base):
    __tablename__ = "ledger_accounts"
    # One seller_payable per artist; one row per platform account type (owner_id NULL).
    # Postgres treats NULLs as distinct in a plain unique index, so the platform singletons
    # are additionally guarded by a partial unique index created in the migration.
    __table_args__ = (UniqueConstraint("type", "owner_id", name="uq_ledger_accounts_type_owner"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type = Column(String(32), nullable=False, index=True)
    owner_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    currency = Column(String(3), default="USD", nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    type = Column(String(32), nullable=False, index=True)
    description = Column(Text, nullable=True)
    # Deterministic per logical event (e.g. "order_paid:<order_id>") — makes posting the
    # same transaction twice a no-op even if a webhook is delivered more than once.
    idempotency_key = Column(String(255), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_ledger_entries_amount_positive"),
        CheckConstraint("direction IN ('debit', 'credit')", name="ck_ledger_entries_direction"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transaction_id = Column(
        UUID(as_uuid=True),
        ForeignKey("ledger_transactions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    account_id = Column(
        UUID(as_uuid=True),
        ForeignKey("ledger_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Amount is always positive; `direction` carries the sign.
    direction = Column(String(6), nullable=False)
    amount_cents = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
