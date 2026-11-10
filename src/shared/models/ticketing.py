"""Event ticketing: what a host sells, what a buyer ordered, and the admissions it bought.

Built to the ticketing spec of 25 Oct 2026 (BACKEND_CHANGES B22). Money and stock rules live
in modules/events/ticketing_service.py; these are the rows.

* TicketType — a kind of ticket (General Admission, VIP): price (0 = free), stock, a
  per-order limit and an optional sale window. Once one has sold it is closed, never deleted,
  because tickets point at it.
* TicketOrder — one checkout. `kind` is `purchase` (Choose tickets / checkout) or `rsvp` (the
  free one-tap "I'm going", which issues a ticket too, per the spec). `hold_expires_at` is the
  10-minute hold that stops two people buying the last ticket.
* TicketOrderItem — how many of each type the order holds. Rows, not JSON, so stock is a SUM
  under the ticket type's row lock.
* Ticket — one admission. Its QR value is a signature over its id (ticket_codes.py), made on
  demand rather than stored, so a database read alone can never mint a working ticket.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from src.shared.config.database import Base


def utc_now():
    return datetime.now(timezone.utc)


ORDER_KIND_PURCHASE = "purchase"
ORDER_KIND_RSVP = "rsvp"

ORDER_PENDING = "pending_payment"
ORDER_PAID = "paid"
ORDER_EXPIRED = "expired"
ORDER_CANCELLED = "cancelled"
ORDER_FAILED = "failed"
ORDER_REFUNDED = "refunded"
ORDER_PARTLY_REFUNDED = "partly_refunded"

TICKET_VALID = "valid"
TICKET_CHECKED_IN = "checked_in"
TICKET_REFUNDED = "refunded"
TICKET_VOID = "void"
# Tickets that still admit someone, and so still use up stock.
LIVE_TICKET_STATUSES = (TICKET_VALID, TICKET_CHECKED_IN)

# The name a ticket from a plain RSVP (an event with no ticket types) carries.
RSVP_TICKET_NAME = "General Admission"


class TicketType(Base):
    __tablename__ = "event_ticket_types"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    name = Column(String(80), nullable=False)
    price_cents = Column(Integer, nullable=False)
    includes = Column(Text, nullable=True)
    # Null = no cap.
    quantity = Column(Integer, nullable=True)
    max_per_order = Column(Integer, nullable=True)
    sale_starts_at = Column(DateTime(timezone=True), nullable=True)
    sale_ends_at = Column(DateTime(timezone=True), nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    # Set when the host takes a sold type off sale. Its tickets stay valid.
    closed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    @property
    def is_free(self) -> bool:
        return self.price_cents == 0


class TicketOrder(Base):
    __tablename__ = "event_ticket_orders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id", ondelete="RESTRICT"),
                      nullable=False, index=True)
    buyer_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
                      nullable=False, index=True)
    kind = Column(String(12), nullable=False, default=ORDER_KIND_PURCHASE)
    # Where the tickets are emailed (the spec's checkout field); the account email by default.
    email = Column(String(255), nullable=True)
    subtotal_cents = Column(Integer, nullable=False)
    tax_cents = Column(Integer, nullable=False)
    total_cents = Column(Integer, nullable=False)
    # Studio 3's fee on these tickets, snapshotted at checkout so a later rate change can
    # never restate what the host was promised (the same rule as art orders).
    fee_bps = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default=ORDER_PENDING)
    payment_reference = Column(String(255), nullable=True, unique=True)
    stripe_charge_id = Column(String(255), nullable=True)
    hold_expires_at = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    # What Stripe kept on the payment. Not returned on a refund, so a host who cancels pays it
    # (spec: "the host pays the card processing fees").
    stripe_fee_cents = Column(Integer, nullable=True)
    # How much has gone back, split the way the ledger booked it, so the last refund of an
    # order returns exactly what is left (no cent lost to rounding a ticket at a time).
    refunded_subtotal_cents = Column(Integer, nullable=False, default=0, server_default="0")
    refunded_tax_cents = Column(Integer, nullable=False, default=0, server_default="0")
    refunded_fee_cents = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    @property
    def refunded_cents(self) -> int:
        return (self.refunded_subtotal_cents or 0) + (self.refunded_tax_cents or 0)


class TicketOrderItem(Base):
    __tablename__ = "event_ticket_order_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(UUID(as_uuid=True), ForeignKey("event_ticket_orders.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    # Null only for an RSVP on an event with no ticket types.
    ticket_type_id = Column(UUID(as_uuid=True),
                            ForeignKey("event_ticket_types.id", ondelete="RESTRICT"),
                            nullable=True, index=True)
    quantity = Column(Integer, nullable=False)
    unit_price_cents = Column(Integer, nullable=False)


class Ticket(Base):
    __tablename__ = "event_tickets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(UUID(as_uuid=True), ForeignKey("event_ticket_orders.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    ticket_type_id = Column(UUID(as_uuid=True),
                            ForeignKey("event_ticket_types.id", ondelete="RESTRICT"),
                            nullable=True, index=True)
    holder_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    # Short, human-typeable ("S3-T-8KQ2X"), for the door when a QR won't scan.
    number = Column(String(16), nullable=False, unique=True)
    status = Column(String(16), nullable=False, default=TICKET_VALID)
    checked_in_at = Column(DateTime(timezone=True), nullable=True)
    checked_in_by_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
                              nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    # When the ticket's wallet pass last changed (Apple asks "what changed since X").
    pass_updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)


class WalletAppleRegistration(Base):
    """A device holding a ticket's Apple Wallet pass, registered through the pass web service.
    Pushed (with its token) whenever that ticket's pass changes."""

    __tablename__ = "wallet_apple_registrations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_library_id = Column(String(255), nullable=False, index=True)
    push_token = Column(String(255), nullable=False)
    pass_type_id = Column(String(255), nullable=False)
    ticket_id = Column(UUID(as_uuid=True), ForeignKey("event_tickets.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


# --- waitlist (spec "Waitlist") ----------------------------------------------------------------

WAITLIST_WAITING = "waiting"
WAITLIST_HELD = "held"
WAITLIST_BOUGHT = "bought"
WAITLIST_EXPIRED = "expired"
WAITLIST_LEFT = "left"
# The event started (or was cancelled) with them still in line: removed quietly.
WAITLIST_CLOSED = "closed"
WAITLIST_IN_LINE = (WAITLIST_WAITING, WAITLIST_HELD)

HOLD_ACTIVE = "active"
# Being paid for: the checkout's own 10-minute hold now covers these tickets.
HOLD_CHECKOUT = "checkout"
HOLD_USED = "used"
HOLD_RELEASED = "released"
HOLD_EXPIRED = "expired"
HOLD_CANCELLED = "cancelled"
# Holds that keep tickets away from everyone else.
HOLDING_STATUSES = (HOLD_ACTIVE,)


class WaitlistEntry(Base):
    """Someone in line for a sold-out event, and how many of each type they want."""

    __tablename__ = "event_waitlist_entries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(12), nullable=False, default=WAITLIST_WAITING)
    # {ticket type id (str): count}. Read and written whole; a handful of keys at most.
    wanted = Column(JSONB, nullable=False, default=dict)
    # First come, first served by this. Reset when someone who left joins again.
    joined_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class WaitlistHold(Base):
    """Tickets set aside for the next person in line, until `held_until` (spec: 2 hours, 30
    minutes or 10 minutes depending on how soon the event starts)."""

    __tablename__ = "event_waitlist_holds"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_id = Column(UUID(as_uuid=True), ForeignKey("event_waitlist_entries.id", ondelete="CASCADE"),
                      nullable=False)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    ticket_type_id = Column(UUID(as_uuid=True), ForeignKey("event_ticket_types.id", ondelete="CASCADE"),
                            nullable=False)
    quantity = Column(Integer, nullable=False)
    held_until = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(12), nullable=False, default=HOLD_ACTIVE)
    order_id = Column(UUID(as_uuid=True), ForeignKey("event_ticket_orders.id", ondelete="SET NULL"),
                      nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


# --- the host's money --------------------------------------------------------------------------

EVENT_PAYOUT_PENDING = "pending"
EVENT_PAYOUT_SENDING = "sending"
EVENT_PAYOUT_RELEASED = "released"
EVENT_PAYOUT_FAILED = "transfer_failed"
# Nothing to send: every ticket was free or refunded, or card-fee debts ate it all.
EVENT_PAYOUT_NOTHING = "nothing_owed"


class EventPayout(Base):
    """The host's ticket money for one event, transferred once the event is over (T-1)."""

    __tablename__ = "event_payouts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id", ondelete="RESTRICT"),
                      nullable=False, unique=True)
    host_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    owed_cents = Column(Integer, nullable=False)
    debt_recovered_cents = Column(Integer, nullable=False, default=0)
    amount_cents = Column(Integer, nullable=False)
    status = Column(String(24), nullable=False, default=EVENT_PAYOUT_PENDING)
    stripe_transfer_id = Column(String(255), nullable=True, unique=True)
    idempotency_key = Column(String(255), nullable=False, unique=True)
    ledger_transaction_id = Column(UUID(as_uuid=True), ForeignKey("ledger_transactions.id",
                                                                  ondelete="RESTRICT"), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    failure_reason = Column(Text, nullable=True)
    released_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


DEBT_OPEN = "open"
DEBT_SETTLED = "settled"
DEBT_WAIVED = "waived"


class HostFeeDebt(Base):
    """Card fees a host owes for refunding everyone when they cancelled (spec: "Take them from
    the host's next payout, or charge the host if there is no payout")."""

    __tablename__ = "host_fee_debts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id", ondelete="RESTRICT"),
                      nullable=False, unique=True)
    amount_cents = Column(Integer, nullable=False)
    recovered_cents = Column(Integer, nullable=False, default=0)
    status = Column(String(12), nullable=False, default=DEBT_OPEN)
    note = Column(Text, nullable=True)
    settled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    @property
    def outstanding_cents(self) -> int:
        return max(0, self.amount_cents - (self.recovered_cents or 0))
