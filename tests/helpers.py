"""Assertions and small utilities shared across the money tests."""
import threading
import uuid
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.shared.ledger import ledger_service
from src.shared.models.ledger import CREDIT, DEBIT, LedgerAccount, LedgerEntry


def assert_ledger_balanced(db: Session) -> None:
    """Total debits equal total credits across the whole ledger.

    The cheapest high-value assertion in the suite: every money test ends with it, and any
    transaction that writes one side without the other trips it regardless of which code
    path produced the imbalance.
    """
    debits = db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(
            LedgerEntry.direction == DEBIT
        )
    ).scalar_one()
    credits = db.execute(
        select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(
            LedgerEntry.direction == CREDIT
        )
    ).scalar_one()
    assert debits == credits, f"Ledger out of balance: debits {debits} != credits {credits}"


def platform_balance(db: Session, account_type: str) -> int:
    """Credits minus debits for a platform singleton account."""
    account = db.execute(
        select(LedgerAccount).where(
            LedgerAccount.type == account_type, LedgerAccount.owner_id.is_(None)
        )
    ).scalar_one()
    return ledger_service.get_balance(db, account)


def seller_balance(db: Session, seller_id: uuid.UUID) -> int:
    return ledger_service.get_seller_balance(db, seller_id)


def run_concurrently(*fns: Callable[[], object]) -> list:
    """Run callables on real OS threads and return their results in order.

    Real threads, not greenlets: these tests exist to prove that Postgres row locks
    serialise two genuinely concurrent writers, which cooperative scheduling would never
    exercise. Exceptions are returned rather than raised so the caller can assert that
    exactly one of two racers failed.
    """
    results: list = [None] * len(fns)

    def _run(index: int, fn: Callable[[], object]) -> None:
        try:
            results[index] = fn()
        except BaseException as exc:  # noqa: BLE001 — the exception IS the result here
            results[index] = exc

    threads = [threading.Thread(target=_run, args=(i, fn)) for i, fn in enumerate(fns)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    for thread in threads:
        assert not thread.is_alive(), "A concurrent worker deadlocked (30s timeout)"
    return results
