"""Admin queries — cross-user reads the normal DAOs deliberately don't expose.

Milestone 1 covers the order list/detail the ops queue is built on; dispute resolution and
payout retry queries land with Milestone 5.
"""
import uuid
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from src.shared.models.order import Order, OrderItem
from src.shared.models.user import User
from src.shared.models.piece import Piece
from src.shared.models.payout import PAYOUT_BLOCKED, PAYOUT_TRANSFER_FAILED, Payout
from src.shared.models.shipment import Shipment
from src.shared.models.dispute import Dispute
from src.shared.models.ledger import LedgerEntry, LedgerTransaction, LedgerAccount


def list_orders(
    db: Session, status: Optional[str] = None, limit: int = 50, offset: int = 0
) -> list[Order]:
    q = select(Order)
    if status:
        q = q.where(Order.status == status)
    return list(
        db.execute(q.order_by(Order.created_at.desc()).limit(limit).offset(offset)).scalars().all()
    )


def count_orders(db: Session, status: Optional[str] = None) -> int:
    q = select(func.count(Order.id))
    if status:
        q = q.where(Order.status == status)
    return db.execute(q).scalar_one()


def count_by_status(db: Session) -> dict:
    rows = db.execute(select(Order.status, func.count(Order.id)).group_by(Order.status)).all()
    return {status: count for status, count in rows}


def get_order_full(db: Session, order_id: uuid.UUID) -> Optional[dict]:
    """Everything the ops team needs on one screen: the order, both parties, the artwork,
    and the shipment/payout/dispute plus the ledger entries that explain where the money is.
    """
    order = db.get(Order, order_id)
    if not order:
        return None

    items = list(db.execute(select(OrderItem).where(OrderItem.order_id == order.id)).scalars().all())
    pieces = [db.get(Piece, i.piece_id) for i in items]

    return {
        "order": order,
        "buyer": db.get(User, order.buyer_id),
        "seller": db.get(User, order.seller_id),
        "items": items,
        "pieces": [p for p in pieces if p],
        "shipment": db.execute(
            select(Shipment).where(Shipment.order_id == order.id)
        ).scalar_one_or_none(),
        "payout": db.execute(
            select(Payout).where(Payout.order_id == order.id)
        ).scalar_one_or_none(),
        "dispute": db.execute(
            select(Dispute).where(Dispute.order_id == order.id)
        ).scalar_one_or_none(),
        "ledger": list_ledger_for_order(db, order.id),
    }


def list_disputes(db: Session, status: Optional[str] = "open") -> list[dict]:
    """Dispute queue. Joined with the order so the queue can show amounts at risk without
    an N+1 per row."""
    q = select(Dispute, Order).join(Order, Order.id == Dispute.order_id)
    if status:
        q = q.where(Dispute.status == status)
    rows = db.execute(q.order_by(Dispute.created_at.asc())).all()
    return [{"dispute": d, "order": o} for d, o in rows]


def count_open_disputes(db: Session) -> int:
    return db.execute(
        select(func.count(Dispute.id)).where(Dispute.status == "open")
    ).scalar_one()


# Payout states that mean an artist is unpaid on a delivered order and nobody has
# decided what happens next.
ATTENTION_PAYOUT_STATUSES = (PAYOUT_TRANSFER_FAILED, PAYOUT_BLOCKED)


def list_failed_payouts(db: Session) -> list[dict]:
    """Payouts needing ops attention — a failed transfer means an artist is unpaid on a
    delivered order, so this is the other queue that must not go unwatched.

    Includes `blocked` as well as `transfer_failed`: blocked is the deliberate hold applied
    by a chargeback or an admin refund. It is not retryable, but it absolutely still needs a
    human to look at it, and filtering on transfer_failed alone hid it entirely.
    """
    rows = db.execute(
        select(Payout, Order, User)
        .join(Order, Order.id == Payout.order_id)
        .join(User, User.id == Payout.seller_id)
        .where(Payout.status.in_(ATTENTION_PAYOUT_STATUSES))
        .order_by(Payout.updated_at.desc())
    ).all()
    return [{"payout": p, "order": o, "seller": u} for p, o, u in rows]


def count_failed_payouts(db: Session) -> int:
    return db.execute(
        select(func.count(Payout.id)).where(Payout.status.in_(ATTENTION_PAYOUT_STATUSES))
    ).scalar_one()


def list_ledger_for_order(db: Session, order_id: uuid.UUID) -> list[dict]:
    """Flat list of ledger entries for an order — the 'where did the money go' answer."""
    rows = db.execute(
        select(LedgerTransaction, LedgerEntry, LedgerAccount)
        .join(LedgerEntry, LedgerEntry.transaction_id == LedgerTransaction.id)
        .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
        .where(LedgerTransaction.order_id == order_id)
        .order_by(LedgerTransaction.created_at, LedgerEntry.direction)
    ).all()
    return [
        {
            "transactionType": txn.type,
            "createdAt": txn.created_at,
            "account": account.type,
            "direction": entry.direction,
            "amountCents": entry.amount_cents,
        }
        for txn, entry, account in rows
    ]
