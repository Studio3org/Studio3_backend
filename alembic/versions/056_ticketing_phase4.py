"""Ticketing, Phase 4: refunds, the waitlist, rescheduling, host payouts and host fee debts.

Revision ID: 056_ticketing_phase4
Revises: 055_wallet_passes

Built to the ticketing spec of 25 Oct 2026 (BACKEND_CHANGES B22, B26, B28).

* event_waitlist_entries: one row per person per event, flipped between waiting, held,
  bought, expired, left and closed (as RSVPs are). `wanted` is {ticket type id: count}.
* event_waitlist_holds: tickets set aside for the next person in line, until `held_until`.
  An active hold counts against stock exactly like a checkout hold.
* events: when the host last rescheduled and the message they sent; when the event was last
  seen sold out and when its savers were last told it reopened (B26).
* event_ticket_orders: the Stripe fee on the payment (what the host pays if they cancel) and
  how much of the order has been refunded, split the way the ledger books it.
* refunds: a ticket order can be refunded a ticket at a time, so a payment can now have more
  than one refund. `refund_key` takes over as the duplicate guard; `ticket_ids` records which
  tickets a refund paid back.
* event_payouts: the host's ticket money, transferred after the event (one per event).
* host_fee_debts: card fees a host owes after cancelling an event, recovered from their next
  ticket payout.
* ledger: a `host_receivable` platform account for those debts.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "056_ticketing_phase4"
down_revision: Union[str, None] = "055_wallet_passes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    ]


def upgrade() -> None:
    # --- waitlist -------------------------------------------------------------------------
    op.create_table(
        "event_waitlist_entries",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("event_id", UUID, sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="waiting"),
        sa.Column("wanted", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        *_timestamps(),
        sa.UniqueConstraint("event_id", "user_id", name="uq_waitlist_event_user"),
        sa.CheckConstraint(
            "status IN ('waiting','held','bought','expired','left','closed')",
            name="ck_waitlist_entries_status",
        ),
    )
    op.create_index("ix_waitlist_entries_queue", "event_waitlist_entries",
                    ["event_id", "status", "joined_at"])
    op.create_index("ix_waitlist_entries_user", "event_waitlist_entries", ["user_id"])

    op.create_table(
        "event_waitlist_holds",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("entry_id", UUID, sa.ForeignKey("event_waitlist_entries.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("event_id", UUID, sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ticket_type_id", UUID,
                  sa.ForeignKey("event_ticket_types.id", ondelete="CASCADE"), nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("held_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="active"),
        # The checkout using this hold, while it is being paid for.
        sa.Column("order_id", UUID, sa.ForeignKey("event_ticket_orders.id", ondelete="SET NULL"),
                  nullable=True),
        *_timestamps(),
        sa.CheckConstraint("quantity > 0", name="ck_waitlist_holds_quantity"),
        sa.CheckConstraint(
            "status IN ('active','checkout','used','released','expired','cancelled')",
            name="ck_waitlist_holds_status",
        ),
    )
    op.create_index("ix_waitlist_holds_type", "event_waitlist_holds",
                    ["ticket_type_id", "status", "held_until"])
    op.create_index("ix_waitlist_holds_entry", "event_waitlist_holds", ["entry_id"])
    op.create_index("ix_waitlist_holds_event_user", "event_waitlist_holds", ["event_id", "user_id"])
    op.create_index("ix_waitlist_holds_order", "event_waitlist_holds", ["order_id"])

    # --- events ---------------------------------------------------------------------------
    op.add_column("events", sa.Column("rescheduled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("events", sa.Column("reschedule_message", sa.Text, nullable=True))
    op.add_column("events", sa.Column("sold_out_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("events", sa.Column("savers_notified_at", sa.DateTime(timezone=True),
                                      nullable=True))

    # --- ticket orders --------------------------------------------------------------------
    op.add_column("event_ticket_orders", sa.Column("stripe_fee_cents", sa.Integer, nullable=True))
    for column in ("refunded_subtotal_cents", "refunded_tax_cents", "refunded_fee_cents"):
        op.add_column("event_ticket_orders", sa.Column(column, sa.Integer, nullable=False,
                                                       server_default="0"))

    # --- refunds: several per payment, one per key ----------------------------------------
    op.drop_constraint("refunds_payment_intent_id_key", "refunds", type_="unique")
    op.create_index("ix_refunds_payment_intent_id", "refunds", ["payment_intent_id"])
    op.add_column("refunds", sa.Column("refund_key", sa.String(255), nullable=True))
    op.create_unique_constraint("uq_refunds_refund_key", "refunds", ["refund_key"])
    op.add_column("refunds", sa.Column("ticket_ids", postgresql.JSONB, nullable=True))
    op.drop_constraint("ck_refunds_leg", "refunds", type_="check")
    op.create_check_constraint("ck_refunds_leg", "refunds",
                               "leg IN ('charge', 'prepaid', 'late', 'tickets')")

    # --- host payouts and debts -----------------------------------------------------------
    op.create_table(
        "event_payouts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("event_id", UUID, sa.ForeignKey("events.id", ondelete="RESTRICT"),
                  nullable=False, unique=True),
        sa.Column("host_id", UUID, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        # What the host earned from this event, what was kept back for card fees they owe
        # from an earlier cancellation, and what was transferred (owed - recovered).
        sa.Column("owed_cents", sa.Integer, nullable=False),
        sa.Column("debt_recovered_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("stripe_transfer_id", sa.String(255), nullable=True, unique=True),
        sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
        sa.Column("ledger_transaction_id", UUID,
                  sa.ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_event_payouts_host", "event_payouts", ["host_id"])
    op.create_index("ix_event_payouts_status", "event_payouts", ["status"])

    op.create_table(
        "host_fee_debts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("host_id", UUID, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("event_id", UUID, sa.ForeignKey("events.id", ondelete="RESTRICT"),
                  nullable=False, unique=True),
        sa.Column("amount_cents", sa.Integer, nullable=False),
        sa.Column("recovered_cents", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(12), nullable=False, server_default="open"),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("status IN ('open','settled','waived')", name="ck_host_fee_debts_status"),
        sa.CheckConstraint("recovered_cents <= amount_cents", name="ck_host_fee_debts_recovered"),
    )
    op.create_index("ix_host_fee_debts_host_status", "host_fee_debts", ["host_id", "status"])

    ledger_accounts = sa.table(
        "ledger_accounts",
        sa.column("id", UUID), sa.column("type", sa.String), sa.column("owner_id", UUID),
        sa.column("currency", sa.String), sa.column("created_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(ledger_accounts, [{
        "id": uuid.uuid4(), "type": "host_receivable", "owner_id": None, "currency": "USD",
        "created_at": datetime.now(timezone.utc),
    }])


def downgrade() -> None:
    op.execute("DELETE FROM ledger_accounts WHERE type = 'host_receivable'")
    op.drop_index("ix_host_fee_debts_host_status", table_name="host_fee_debts")
    op.drop_table("host_fee_debts")
    op.drop_index("ix_event_payouts_status", table_name="event_payouts")
    op.drop_index("ix_event_payouts_host", table_name="event_payouts")
    op.drop_table("event_payouts")

    op.execute("DELETE FROM refunds WHERE leg = 'tickets'")
    op.drop_constraint("ck_refunds_leg", "refunds", type_="check")
    op.create_check_constraint("ck_refunds_leg", "refunds", "leg IN ('charge', 'prepaid', 'late')")
    op.drop_column("refunds", "ticket_ids")
    op.drop_constraint("uq_refunds_refund_key", "refunds", type_="unique")
    op.drop_column("refunds", "refund_key")
    op.drop_index("ix_refunds_payment_intent_id", table_name="refunds")
    # Restoring the old guard fails if a payment now has two refunds, which is the honest
    # outcome: those rows cannot be represented in the older schema.
    op.create_unique_constraint("refunds_payment_intent_id_key", "refunds", ["payment_intent_id"])

    for column in ("refunded_fee_cents", "refunded_tax_cents", "refunded_subtotal_cents",
                   "stripe_fee_cents"):
        op.drop_column("event_ticket_orders", column)
    for column in ("savers_notified_at", "sold_out_at", "reschedule_message", "rescheduled_at"):
        op.drop_column("events", column)

    for index in ("ix_waitlist_holds_order", "ix_waitlist_holds_event_user",
                  "ix_waitlist_holds_entry", "ix_waitlist_holds_type"):
        op.drop_index(index, table_name="event_waitlist_holds")
    op.drop_table("event_waitlist_holds")
    op.drop_index("ix_waitlist_entries_user", table_name="event_waitlist_entries")
    op.drop_index("ix_waitlist_entries_queue", table_name="event_waitlist_entries")
    op.drop_table("event_waitlist_entries")
