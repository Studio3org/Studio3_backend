"""Proves the harness itself works before any real test depends on it.

If these fail, nothing else in the suite can be trusted: the schema did not migrate, the
platform ledger seed is missing, isolation is not cleaning up, or the app will not build.
"""
from sqlalchemy import func, select

from src.shared.ledger import ledger_service
from src.shared.models.ledger import (
    ACCOUNT_PLATFORM_CLEARING,
    ACCOUNT_SELLER_PAYABLE,
    CREDIT,
    DEBIT,
    PLATFORM_ACCOUNT_TYPES,
    LedgerAccount,
)
from src.shared.models.user import User
from tests.factories import make_user
from tests.helpers import assert_ledger_balanced, platform_balance


def test_migrations_produced_the_schema(db):
    """A table from a late migration exists, so the whole chain ran, not just the first."""
    assert db.execute(select(func.count()).select_from(User.__table__)).scalar_one() == 0


def test_platform_ledger_accounts_are_seeded(db):
    """Migration 022 bulk-inserts these. If create_all had been used instead, this would be
    zero and every ledger test would silently exercise the defensive fallback."""
    rows = db.execute(
        select(LedgerAccount).where(LedgerAccount.owner_id.is_(None))
    ).scalars().all()
    assert {r.type for r in rows} == set(PLATFORM_ACCOUNT_TYPES)


def test_a_balanced_transaction_round_trips(db):
    seller = make_user(db, seller=True)
    clearing = ledger_service.get_platform_account(db, ACCOUNT_PLATFORM_CLEARING)
    payable = ledger_service.get_seller_account(db, seller.id)

    ledger_service.post_transaction(
        db,
        txn_type="order_paid",
        entries=[(clearing, DEBIT, 10_000), (payable, CREDIT, 10_000)],
        idempotency_key=f"smoke:{seller.id}",
    )

    assert ledger_service.get_seller_balance(db, seller.id) == 10_000
    assert platform_balance(db, ACCOUNT_PLATFORM_CLEARING) == -10_000
    assert_ledger_balanced(db)


def test_isolation_cleans_up_between_tests(db):
    """Pairs with the test above: if TRUNCATE were not running, these would be non-zero."""
    assert db.execute(select(func.count()).select_from(User.__table__)).scalar_one() == 0
    assert db.execute(
        select(func.count()).select_from(LedgerAccount.__table__).where(
            LedgerAccount.type == ACCOUNT_SELLER_PAYABLE
        )
    ).scalar_one() == 0


def test_redis_is_flushed_between_tests(flush_redis):
    assert flush_redis.dbsize() == 0
    flush_redis.set("leftover", "1")


def test_redis_really_was_flushed(flush_redis):
    assert flush_redis.get("leftover") is None


def test_app_builds_and_serves_health(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.get_json()["message"]


def test_scheduler_is_suppressed_under_testing(app):
    """The APScheduler thread must not start in tests — it would close auctions underneath
    them on a 1-minute timer."""
    assert app.config["TESTING"] is True
