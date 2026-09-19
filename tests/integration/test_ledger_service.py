"""Ledger invariants: balance, idempotency, and not destroying the caller's transaction."""
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.shared.config.database import SessionLocal
from src.shared.ledger import ledger_service
from src.shared.ledger.ledger_service import LedgerError
from src.shared.models.ledger import (
    ACCOUNT_PLATFORM_CLEARING,
    ACCOUNT_SELLER_PAYABLE,
    CREDIT,
    DEBIT,
    LedgerAccount,
    LedgerEntry,
)
from src.shared.models.order import Order
from tests.factories import make_order, make_user
from tests.helpers import assert_ledger_balanced


def _accounts(db, seller_id):
    return (
        ledger_service.get_platform_account(db, ACCOUNT_PLATFORM_CLEARING),
        ledger_service.get_seller_account(db, seller_id),
    )


def test_racing_account_create_preserves_the_callers_pending_work(db, monkeypatch):
    """Regression: recovering from a concurrent account insert must not roll back the caller.

    get_seller_account caught the unique-constraint violation with db.rollback(), which
    discards the WHOLE transaction — including the order transition, piece status writes and
    payout insert that book_order_paid runs after. The caller then committed an empty
    transaction and returned 200, so Stripe never retried and the order stayed unpaid.
    """
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    order = make_order(db, buyer=buyer, seller=seller, artwork_cents=100_00)

    # Another worker wins the race and commits the account first.
    other = SessionLocal()
    try:
        ledger_service.get_seller_account(other, seller.id)
        other.commit()
    finally:
        other.close()

    # Force the collision deterministically: make the caller's lookup miss once, exactly as
    # it would if its SELECT had run before the other worker committed.
    real_find = ledger_service._find_seller_account
    calls = {"n": 0}

    def _miss_once(session, seller_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_find(session, seller_id)

    monkeypatch.setattr(ledger_service, "_find_seller_account", _miss_once)

    caller = SessionLocal()
    try:
        pending = caller.get(Order, order.id)
        pending.status = "paid"  # the caller's in-flight work
        caller.flush()

        account = ledger_service.get_seller_account(caller, seller.id)
        assert account.owner_id == seller.id
        caller.commit()
    finally:
        caller.close()

    db.expire_all()
    assert db.get(Order, order.id).status == "paid", "caller's pending work was discarded"
    assert db.execute(
        select(func.count()).select_from(LedgerAccount.__table__).where(
            LedgerAccount.type == ACCOUNT_SELLER_PAYABLE, LedgerAccount.owner_id == seller.id
        )
    ).scalar_one() == 1


def test_unbalanced_transaction_raises_and_persists_nothing(db):
    seller = make_user(db, seller=True)
    clearing, payable = _accounts(db, seller.id)

    with pytest.raises(LedgerError, match="Unbalanced"):
        ledger_service.post_transaction(
            db,
            txn_type="order_paid",
            entries=[(clearing, DEBIT, 10_000), (payable, CREDIT, 9_000)],
            idempotency_key="unbalanced",
        )

    db.rollback()
    assert db.execute(select(func.count()).select_from(LedgerEntry.__table__)).scalar_one() == 0


def test_negative_amount_is_rejected(db):
    seller = make_user(db, seller=True)
    clearing, payable = _accounts(db, seller.id)
    with pytest.raises(LedgerError, match="Negative amount"):
        ledger_service.post_transaction(
            db,
            txn_type="order_paid",
            entries=[(clearing, DEBIT, -500), (payable, CREDIT, -500)],
            idempotency_key="negative",
        )


def test_invalid_direction_is_rejected(db):
    seller = make_user(db, seller=True)
    clearing, payable = _accounts(db, seller.id)
    with pytest.raises(LedgerError, match="Invalid ledger direction"):
        ledger_service.post_transaction(
            db,
            txn_type="order_paid",
            entries=[(clearing, "sideways", 500), (payable, CREDIT, 500)],
            idempotency_key="direction",
        )


def test_all_zero_entries_are_rejected(db):
    seller = make_user(db, seller=True)
    clearing, payable = _accounts(db, seller.id)
    with pytest.raises(LedgerError, match="no non-zero entries"):
        ledger_service.post_transaction(
            db,
            txn_type="order_paid",
            entries=[(clearing, DEBIT, 0), (payable, CREDIT, 0)],
            idempotency_key="allzero",
        )


def test_zero_amount_entries_are_dropped_not_written(db):
    """A free-shipping order legitimately has a zero line, and the DB requires amounts > 0."""
    seller = make_user(db, seller=True)
    clearing, payable = _accounts(db, seller.id)

    ledger_service.post_transaction(
        db,
        txn_type="order_paid",
        entries=[(clearing, DEBIT, 5_000), (payable, CREDIT, 5_000), (clearing, CREDIT, 0)],
        idempotency_key="zero-line",
    )

    assert db.execute(select(func.count()).select_from(LedgerEntry.__table__)).scalar_one() == 2
    assert_ledger_balanced(db)


def test_same_idempotency_key_returns_the_existing_transaction(db):
    seller = make_user(db, seller=True)
    clearing, payable = _accounts(db, seller.id)
    entries = [(clearing, DEBIT, 5_000), (payable, CREDIT, 5_000)]

    first = ledger_service.post_transaction(
        db, txn_type="order_paid", entries=entries, idempotency_key="same-key"
    )
    second = ledger_service.post_transaction(
        db, txn_type="order_paid", entries=entries, idempotency_key="same-key"
    )

    assert first.id == second.id
    assert db.execute(select(func.count()).select_from(LedgerEntry.__table__)).scalar_one() == 2
    assert_ledger_balanced(db)


def test_balance_sign_convention_for_a_liability_account(db):
    """seller_payable returns a positive number when money is owed to the artist."""
    seller = make_user(db, seller=True)
    clearing, payable = _accounts(db, seller.id)

    ledger_service.post_transaction(
        db,
        txn_type="order_paid",
        entries=[(clearing, DEBIT, 1_000), (payable, CREDIT, 1_000)],
        idempotency_key="credit-leg",
    )
    ledger_service.post_transaction(
        db,
        txn_type="payout_released",
        entries=[(payable, DEBIT, 400), (clearing, CREDIT, 400)],
        idempotency_key="debit-leg",
    )

    assert ledger_service.get_seller_balance(db, seller.id) == 600
    assert_ledger_balanced(db)


def test_platform_accounts_are_singletons(db):
    """Two lookups of the same platform account must not create a second row."""
    first = ledger_service.get_platform_account(db, ACCOUNT_PLATFORM_CLEARING)
    second = ledger_service.get_platform_account(db, ACCOUNT_PLATFORM_CLEARING)
    assert first.id == second.id
    assert db.execute(
        select(func.count()).select_from(LedgerAccount.__table__).where(
            LedgerAccount.type == ACCOUNT_PLATFORM_CLEARING
        )
    ).scalar_one() == 1


def test_seller_account_is_created_once_per_seller(db):
    seller = make_user(db, seller=True)
    first = ledger_service.get_seller_account(db, seller.id)
    second = ledger_service.get_seller_account(db, seller.id)
    assert first.id == second.id
    # A seller with an account but no entries is owed nothing.
    assert ledger_service.get_seller_balance(db, seller.id) == 0


def test_account_for_an_unknown_seller_raises_rather_than_silently_succeeding(db):
    """The owner_id FK is real. Recovery must re-raise when the row genuinely cannot exist,
    not swallow the error and hand back None."""
    with pytest.raises(IntegrityError):
        ledger_service.get_seller_account(db, uuid.uuid4())
    db.rollback()
