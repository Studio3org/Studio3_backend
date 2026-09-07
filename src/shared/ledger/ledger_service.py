"""The only writer of ledger rows.

Two guarantees this module exists to enforce:

1. **Balance.** Every transaction's debits must equal its credits, checked before commit.
   An unbalanced transaction raises rather than being partially written.
2. **Idempotency.** Posting is keyed on a deterministic `idempotency_key`, so replaying the
   same logical event (a duplicate Stripe webhook, a retried admin action) is a no-op
   instead of double-counting money.

Ledger rows are append-only: nothing here updates or deletes an entry. Corrections are new
reversing transactions, which is what keeps the ledger usable as an audit trail.
"""
import uuid
from typing import Iterable, Optional

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.shared.models.ledger import (
    CREDIT,
    DEBIT,
    LedgerAccount,
    LedgerEntry,
    LedgerTransaction,
    ACCOUNT_SELLER_PAYABLE,
)
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)


class LedgerError(RuntimeError):
    """Raised when a ledger operation would violate an invariant. Not an AppError: an
    unbalanced transaction is a programming bug, not a user-facing condition."""


def get_platform_account(db: Session, account_type: str, currency: str = "USD") -> LedgerAccount:
    """Fetch a platform singleton account (seeded in migration 022)."""
    account = db.execute(
        select(LedgerAccount).where(
            LedgerAccount.type == account_type, LedgerAccount.owner_id.is_(None)
        )
    ).scalar_one_or_none()
    if account:
        return account
    # Defensive: only reachable if the migration seed was rolled back or the row deleted.
    account = LedgerAccount(id=uuid.uuid4(), type=account_type, owner_id=None, currency=currency)
    db.add(account)
    db.flush()
    return account


def get_seller_account(db: Session, seller_id: uuid.UUID, currency: str = "USD") -> LedgerAccount:
    """Get-or-create a seller's payable account."""
    account = db.execute(
        select(LedgerAccount).where(
            LedgerAccount.type == ACCOUNT_SELLER_PAYABLE, LedgerAccount.owner_id == seller_id
        )
    ).scalar_one_or_none()
    if account:
        return account
    account = LedgerAccount(
        id=uuid.uuid4(), type=ACCOUNT_SELLER_PAYABLE, owner_id=seller_id, currency=currency
    )
    db.add(account)
    try:
        db.flush()
    except IntegrityError:
        # Concurrent create — the unique constraint held, so take the winner's row.
        db.rollback()
        account = db.execute(
            select(LedgerAccount).where(
                LedgerAccount.type == ACCOUNT_SELLER_PAYABLE,
                LedgerAccount.owner_id == seller_id,
            )
        ).scalar_one()
    return account


def get_existing_transaction(db: Session, idempotency_key: str) -> Optional[LedgerTransaction]:
    return db.execute(
        select(LedgerTransaction).where(LedgerTransaction.idempotency_key == idempotency_key)
    ).scalar_one_or_none()


def post_transaction(
    db: Session,
    txn_type: str,
    entries: Iterable[tuple],
    idempotency_key: str,
    order_id: Optional[uuid.UUID] = None,
    description: Optional[str] = None,
    commit: bool = True,
) -> LedgerTransaction:
    """Post one balanced transaction.

    `entries` is an iterable of (account, direction, amount_cents). Zero-amount entries are
    dropped rather than rejected — a shipping charge of 0 on a free-shipping order is
    legitimate, and the DB requires amounts be positive.

    Returns the existing transaction unchanged if `idempotency_key` was already used, which
    is what makes replayed webhooks safe.
    """
    existing = get_existing_transaction(db, idempotency_key)
    if existing:
        logger.info("Ledger transaction %s already posted; skipping.", idempotency_key)
        return existing

    cleaned = [(a, d, int(amt)) for a, d, amt in entries if int(amt) != 0]
    if not cleaned:
        raise LedgerError(f"Ledger transaction {idempotency_key} has no non-zero entries.")

    for _, direction, amount in cleaned:
        if direction not in (DEBIT, CREDIT):
            raise LedgerError(f"Invalid ledger direction {direction!r}.")
        if amount < 0:
            raise LedgerError(
                f"Negative amount {amount} in {idempotency_key}; express direction with "
                "debit/credit, not the sign."
            )

    debits = sum(a for _, d, a in cleaned if d == DEBIT)
    credits = sum(a for _, d, a in cleaned if d == CREDIT)
    if debits != credits:
        raise LedgerError(
            f"Unbalanced ledger transaction {idempotency_key}: "
            f"debits {debits} != credits {credits}."
        )

    txn = LedgerTransaction(
        id=uuid.uuid4(),
        order_id=order_id,
        type=txn_type,
        description=description,
        idempotency_key=idempotency_key,
    )
    db.add(txn)
    db.flush()
    for account, direction, amount in cleaned:
        db.add(
            LedgerEntry(
                id=uuid.uuid4(),
                transaction_id=txn.id,
                account_id=account.id,
                direction=direction,
                amount_cents=amount,
            )
        )

    if commit:
        try:
            db.commit()
        except IntegrityError:
            # Another worker posted the same key between our check and commit.
            db.rollback()
            return db.execute(
                select(LedgerTransaction).where(
                    LedgerTransaction.idempotency_key == idempotency_key
                )
            ).scalar_one()
        db.refresh(txn)
    else:
        db.flush()
    logger.info("Posted ledger transaction %s (%s), %d cents.", idempotency_key, txn_type, debits)
    return txn


def get_balance(db: Session, account: LedgerAccount) -> int:
    """Credits minus debits, in cents.

    Sign convention: this returns a positive number for a liability account with money owed
    (seller_payable), which is how callers read "how much do we owe this artist".
    """
    credits = db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(
            LedgerEntry.account_id == account.id, LedgerEntry.direction == CREDIT
        )
    ).scalar_one()
    debits = db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(
            LedgerEntry.account_id == account.id, LedgerEntry.direction == DEBIT
        )
    ).scalar_one()
    return int(credits) - int(debits)


def get_seller_balance(db: Session, seller_id: uuid.UUID) -> int:
    return get_balance(db, get_seller_account(db, seller_id))
