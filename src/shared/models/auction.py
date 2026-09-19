"""Auction and Hold — one run of one auction, and the money committed to it.

Split deliberately across three tables:

* **Auction** owns the run: when it opens and closes, its reserve, whether it soft-closes.
  A piece accumulates auction rows over time, because cancel-then-relist is the only way to
  move a piece onto an event (spec §8) and history must survive that.
* **Bid** is a historical fact. Once placed, only its status changes.
* **Hold** is the money, and has a lifecycle of its own — it can be refreshed, can expire
  independently of the bid, and can fail while the bid stands.
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
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


# --- auction status ---------------------------------------------------------------------
AUCTION_DRAFT = "draft"
AUCTION_LIVE = "live"
# Inside the soft-close window: still accepting bids, but each one extends the end.
AUCTION_CLOSING = "closing"
AUCTION_CLOSED_SOLD = "closed_sold"
AUCTION_CLOSED_RESERVE_NOT_MET = "closed_reserve_not_met"
AUCTION_CLOSED_NO_BIDS = "closed_no_bids"
# The client chose option (c): an auction that ends without a sale waits for the seller to
# decide, rather than auto-relisting or reverting to a fixed price.
AUCTION_NEEDS_SELLER_ACTION = "needs_seller_action"
AUCTION_CANCELLED = "cancelled"

AUCTION_STATUSES = (
    AUCTION_DRAFT, AUCTION_LIVE, AUCTION_CLOSING, AUCTION_CLOSED_SOLD,
    AUCTION_CLOSED_RESERVE_NOT_MET, AUCTION_CLOSED_NO_BIDS,
    AUCTION_NEEDS_SELLER_ACTION, AUCTION_CANCELLED,
)
# Statuses in which a piece may not start a second auction.
AUCTION_RUNNING_STATUSES = (AUCTION_DRAFT, AUCTION_LIVE, AUCTION_CLOSING)

DELIVERY_SHIP = "ship"
DELIVERY_PICKUP = "pickup"


class Auction(Base):
    __tablename__ = "auctions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','live','closing','closed_sold','closed_reserve_not_met',"
            "'closed_no_bids','needs_seller_action','cancelled')",
            name="ck_auctions_status",
        ),
        CheckConstraint("starting_bid_cents >= 100", name="ck_auctions_starting_bid_min"),
        CheckConstraint(
            "reserve_cents IS NULL OR reserve_cents >= starting_bid_cents",
            name="ck_auctions_reserve_above_start",
        ),
        CheckConstraint(
            "duration_days IS NULL OR duration_days BETWEEN 3 AND 14",
            name="ck_auctions_duration_range",
        ),
        CheckConstraint(
            "delivery_mode IS NULL OR delivery_mode IN ('ship','pickup')",
            name="ck_auctions_delivery_mode",
        ),
        CheckConstraint(
            "commission_bps BETWEEN 0 AND 10000", name="ck_auctions_commission_bps_range"
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    piece_id = Column(
        UUID(as_uuid=True), ForeignKey("pieces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seller_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status = Column(String(32), default=AUCTION_DRAFT, server_default=AUCTION_DRAFT, nullable=False)
    starting_bid_cents = Column(Integer, nullable=False)
    # Never exposed to bidders — only whether it has been met.
    reserve_cents = Column(Integer, nullable=True)
    # Null for an event auction, which takes its window from the event.
    duration_days = Column(Integer, nullable=True)
    opens_at = Column(DateTime(timezone=True), nullable=True)
    closes_at = Column(DateTime(timezone=True), nullable=True)
    extended_once = Column(Boolean, default=False, server_default="false", nullable=False)
    # False for event auctions: bidding stops dead so the piece can change hands before the
    # room empties.
    soft_close_enabled = Column(Boolean, default=True, server_default="true", nullable=False)
    event_id = Column(UUID(as_uuid=True), nullable=True)
    delivery_mode = Column(String(16), nullable=True)
    # Snapshotted at listing: the artist sees a net figure before publishing, and a platform
    # rate change mid-auction must not alter it.
    commission_bps = Column(Integer, nullable=False)
    cancelled_reason = Column(String(64), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    @property
    def is_event_auction(self) -> bool:
        return self.event_id is not None

    @property
    def is_pickup(self) -> bool:
        return self.delivery_mode == DELIVERY_PICKUP


# --- hold status ------------------------------------------------------------------------
HOLD_PENDING = "pending"        # intent created, authorisation not confirmed yet
HOLD_HELD = "held"              # funds authorised and capturable
HOLD_CAPTURED = "captured"      # the only state that writes to the ledger
HOLD_RELEASED = "released"      # outbid, cancelled, or reserve not met — nothing charged
HOLD_CAPTURE_FAILED = "capture_failed"
HOLD_EXPIRED = "expired"        # the authorisation lapsed before we captured it
HOLD_FAILED = "failed"          # the card refused the authorisation outright

HOLD_STATUSES = (
    HOLD_PENDING, HOLD_HELD, HOLD_CAPTURED, HOLD_RELEASED,
    HOLD_CAPTURE_FAILED, HOLD_EXPIRED, HOLD_FAILED,
)
# Money is committed in these states, so they are what the refresh sweep and the close
# consider live.
HOLD_LIVE_STATUSES = (HOLD_PENDING, HOLD_HELD)


class Hold(Base):
    __tablename__ = "holds"
    __table_args__ = (
        # One Stripe intent is one hold. This is the duplicate-authorisation guard: a retried
        # place_bid cannot produce a second authorisation on the same card.
        UniqueConstraint("stripe_payment_intent_id", name="uq_holds_stripe_intent"),
        UniqueConstraint("bid_id", name="uq_holds_bid_id"),
        CheckConstraint(
            "status IN ('pending','held','captured','released','capture_failed','expired','failed')",
            name="ck_holds_status",
        ),
        CheckConstraint("amount_cents > 0", name="ck_holds_amount_positive"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bid_id = Column(
        UUID(as_uuid=True), ForeignKey("bids.id", ondelete="CASCADE"), nullable=False
    )
    bidder_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amount_cents = Column(Integer, nullable=False)
    status = Column(String(24), default=HOLD_PENDING, server_default=HOLD_PENDING, nullable=False)
    stripe_payment_intent_id = Column(String(255), nullable=True)
    stripe_payment_method_id = Column(String(255), nullable=True)
    # Read from Stripe rather than assumed: the window varies by network and can change, and
    # the whole refresh mechanism depends on knowing the real deadline.
    capture_before = Column(DateTime(timezone=True), nullable=True)
    refresh_count = Column(Integer, default=0, server_default="0", nullable=False)
    last_error = Column(Text, nullable=True)
    captured_at = Column(DateTime(timezone=True), nullable=True)
    released_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
