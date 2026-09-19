"""Payout release — the highest dollar exposure in the codebase.

A payout is the one place the platform sends real money out. Everything else moves money
in or between ledger accounts; this actually calls Stripe Transfer.create.
"""
import pytest

from src.modules.payments import money, payouts_service
from src.shared.models.payout import (
    PAYOUT_PENDING,
    PAYOUT_READY_TO_RELEASE,
    PAYOUT_RELEASED,
    PAYOUT_TRANSFER_FAILED,
    Payout,
)
from tests.factories import make_order, make_payout, make_piece, make_user
from tests.helpers import assert_ledger_balanced, seller_balance


def _share(order, artwork_cents):
    """Artist share at the rate the order was actually sold at, not today's config."""
    return money.artist_share_cents(artwork_cents, order.commission_bps)


def _completed_order(db, *, seller, buyer, artwork_cents, shipping_cents=0, charge_id=None):
    """A delivered, paid-for order with its ledger booked and a pending payout row.

    Mirrors what the webhook does on payment_intent.succeeded plus the delivery
    confirmation, which is the only state release_payout_for_order will act on.
    """
    piece = make_piece(db, seller, price_cents=artwork_cents, status="sold")
    order = make_order(
        db,
        buyer=buyer,
        seller=seller,
        piece=piece,
        artwork_cents=artwork_cents,
        shipping_cents=shipping_cents,
        status="completed",
        stripe_charge_id=charge_id,
    )
    money.book_order_paid(db, order, stripe_fee_cents=0)
    make_payout(db, order)
    return order


def test_release_pays_only_this_orders_share_when_seller_has_two_completed_orders(
    db, stripe_enabled
):
    """Regression: the payout amount must be THIS order's artist share.

    payouts_service read `get_seller_balance(seller_id)` — the artist's entire outstanding
    payable across every order — so a seller with two completed orders had the first payout
    transfer the sum of both, and the second then found a zero balance and was marked
    failed with "Nothing owed to this artist".
    """
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    order_a = _completed_order(db, seller=seller, buyer=buyer, artwork_cents=900_00)
    order_b = _completed_order(db, seller=seller, buyer=buyer, artwork_cents=500_00)

    expected_a = _share(order_a, 900_00)
    expected_b = _share(order_b, 500_00)
    assert seller_balance(db, seller.id) == expected_a + expected_b

    result_a = payouts_service.release_payout_for_order(order_a.id)

    assert result_a["status"] == PAYOUT_RELEASED
    assert result_a["amountCents"] == expected_a, "paid out more than this order was worth"
    assert len(stripe_enabled.transfers) == 1
    assert stripe_enabled.transfers[0]["amount"] == expected_a

    # The second order must still be payable for exactly its own share.
    db.expire_all()
    assert seller_balance(db, seller.id) == expected_b

    result_b = payouts_service.release_payout_for_order(order_b.id)
    assert result_b["status"] == PAYOUT_RELEASED
    assert result_b["amountCents"] == expected_b
    assert len(stripe_enabled.transfers) == 2

    db.expire_all()
    assert seller_balance(db, seller.id) == 0
    assert_ledger_balanced(db)


def test_chargebacked_payout_is_not_released(db, stripe_enabled):
    """Regression: a payout blocked by a chargeback must stay blocked.

    _on_chargeback_opened parks the payout in a non-payable state to stop the artist being
    paid on money the bank is clawing back. RELEASABLE_STATUSES included that same status,
    so the buyer tapping "confirm received" released it anyway.
    """
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    order = _completed_order(db, seller=seller, buyer=buyer, artwork_cents=400_00)

    payout = db.query(Payout).filter_by(order_id=order.id).one()
    payout.status = payouts_service.PAYOUT_BLOCKED
    payout.failure_reason = "Chargeback opened"
    db.commit()

    result = payouts_service.release_payout_for_order(order.id)

    assert result["status"] != PAYOUT_RELEASED
    assert stripe_enabled.transfers == [], "transferred money that is under chargeback"
    db.expire_all()
    assert db.query(Payout).filter_by(order_id=order.id).one().status == (
        payouts_service.PAYOUT_BLOCKED
    )


def test_double_release_creates_exactly_one_transfer(db, stripe_enabled):
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    order = _completed_order(db, seller=seller, buyer=buyer, artwork_cents=200_00)

    first = payouts_service.release_payout_for_order(order.id)
    second = payouts_service.release_payout_for_order(order.id)

    assert first["status"] == PAYOUT_RELEASED
    assert second["status"] == PAYOUT_RELEASED
    assert len(stripe_enabled.transfers) == 1
    db.expire_all()
    assert seller_balance(db, seller.id) == 0
    assert_ledger_balanced(db)


def test_crash_recovery_adopts_an_existing_transfer(db, stripe_enabled):
    """If we died after calling Stripe but before recording the id, the retry must adopt the
    transfer that already exists rather than creating a second one."""
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    order = _completed_order(db, seller=seller, buyer=buyer, artwork_cents=300_00)
    expected = _share(order, 300_00)

    # Stripe holds a transfer; our row never learned about it.
    stripe_enabled._transfer_create(
        amount=expected,
        currency="usd",
        destination=seller.stripe_account_id,
        transfer_group=payouts_service.transfer_group(order.id),
    )
    payout = db.query(Payout).filter_by(order_id=order.id).one()
    payout.status = PAYOUT_READY_TO_RELEASE
    db.commit()

    result = payouts_service.release_payout_for_order(order.id)

    assert result["status"] == PAYOUT_RELEASED
    assert len(stripe_enabled.transfers) == 1, "created a second transfer for the same order"
    assert_ledger_balanced(db)


def test_release_refused_when_order_is_not_completed(db, stripe_enabled):
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    order = _completed_order(db, seller=seller, buyer=buyer, artwork_cents=100_00)
    order.status = "shipped"
    db.commit()

    result = payouts_service.release_payout_for_order(order.id)

    assert result["status"] != PAYOUT_RELEASED
    assert result["reason"] == "Order is not completed."
    assert stripe_enabled.transfers == []


@pytest.mark.parametrize(
    "field,value,expected_reason_fragment",
    [
        ("stripe_account_id", None, "Connect onboarding"),
        ("stripe_payouts_enabled", False, "cannot receive payouts"),
    ],
)
def test_release_blocked_when_seller_cannot_receive(
    db, stripe_enabled, field, value, expected_reason_fragment
):
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    order = _completed_order(db, seller=seller, buyer=buyer, artwork_cents=100_00)
    setattr(seller, field, value)
    db.commit()

    result = payouts_service.release_payout_for_order(order.id)

    assert result["status"] == PAYOUT_TRANSFER_FAILED
    assert expected_reason_fragment in result["reason"]
    assert stripe_enabled.transfers == []
    # Nothing was paid, so the artist is still owed the full amount.
    db.expire_all()
    assert seller_balance(db, seller.id) == _share(order, 100_00)
    assert_ledger_balanced(db)


def test_stripe_failure_marks_failed_then_retry_succeeds_once(db, stripe_enabled):
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    order = _completed_order(db, seller=seller, buyer=buyer, artwork_cents=250_00)

    stripe_enabled.transfer_error = RuntimeError("insufficient funds")
    failed = payouts_service.release_payout_for_order(order.id)
    assert failed["status"] == PAYOUT_TRANSFER_FAILED
    assert stripe_enabled.transfers == []

    retried = payouts_service.retry_payout(order.id)

    assert retried["status"] == PAYOUT_RELEASED
    assert len(stripe_enabled.transfers) == 1
    assert retried["amountCents"] == _share(order, 250_00)
    assert_ledger_balanced(db)


def test_dev_mode_books_the_ledger_without_stripe(db, stripe_stub):
    """No Stripe keys configured: the escrow flow must still be exercisable end to end."""
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    order = _completed_order(db, seller=seller, buyer=buyer, artwork_cents=150_00)

    result = payouts_service.release_payout_for_order(order.id)

    assert result["status"] == PAYOUT_RELEASED
    assert result["transferId"] == f"dev_transfer_{order.id}"
    assert stripe_stub.transfers == [], "dev mode must not call Stripe"
    db.expire_all()
    assert seller_balance(db, seller.id) == 0
    assert_ledger_balanced(db)


def test_missing_payout_row_is_reported_not_raised(db, stripe_enabled):
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    piece = make_piece(db, seller, price_cents=100_00, status="sold")
    order = make_order(
        db, buyer=buyer, seller=seller, piece=piece, artwork_cents=100_00, status="completed"
    )

    result = payouts_service.release_payout_for_order(order.id)

    assert result["status"] == "missing"
    assert stripe_enabled.transfers == []


def test_payout_row_starts_pending(db):
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    order = _completed_order(db, seller=seller, buyer=buyer, artwork_cents=100_00)
    assert db.query(Payout).filter_by(order_id=order.id).one().status == PAYOUT_PENDING


def test_blocked_payouts_are_unreleasable_but_still_visible_to_ops(db):
    """The pairing that was wrong: a chargeback parked payouts in a status that was both
    releasable AND absent from the admin queue, so the money could go out and nobody would
    see it was meant to be held."""
    from src.modules.admin.admin_dao import ATTENTION_PAYOUT_STATUSES
    from src.shared.models.payout import PAYOUT_BLOCKED

    assert PAYOUT_BLOCKED not in payouts_service.RELEASABLE_STATUSES
    assert PAYOUT_BLOCKED in ATTENTION_PAYOUT_STATUSES
    # A genuine transfer failure stays retryable, and also stays on the queue.
    assert PAYOUT_TRANSFER_FAILED in payouts_service.RELEASABLE_STATUSES
    assert PAYOUT_TRANSFER_FAILED in ATTENTION_PAYOUT_STATUSES


def test_a_chargeback_removes_a_payout_from_the_releasable_set(db, client, stripe_stub):
    """End to end through the webhook, rather than by setting the status by hand."""
    from src.modules.admin import admin_dao
    from src.shared.models.payout import PAYOUT_BLOCKED
    from tests.stripe_fixtures import make_event, post_webhook

    seller = make_user(db, seller=True)
    buyer = make_user(db)
    order = _completed_order(
        db, seller=seller, buyer=buyer, artwork_cents=300_00, charge_id="ch_cb_1"
    )

    response = post_webhook(
        client, make_event("charge.dispute.created", {"id": "dp_1", "charge": "ch_cb_1"})
    )
    assert response.status_code == 200

    db.expire_all()
    payout = db.query(Payout).filter_by(order_id=order.id).one()
    assert payout.status == PAYOUT_BLOCKED
    assert "chargeback" in payout.failure_reason.lower()
    assert admin_dao.count_failed_payouts(db) == 1, "blocked payout vanished from the ops queue"
