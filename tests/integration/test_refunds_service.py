"""Phase 2: one refund path, the payout held before Stripe is called, honest fee accounting.

Covers PAY-4 (a refund racing the artist's payout), BE-1 (the refund ledger treating the
Stripe fee as returned) and the shipped-order refund that Stripe completed but the order
state machine then refused to record.
"""
import pytest
from sqlalchemy import select

from src.modules.admin import disputes_service
from src.modules.payments import money, payouts_service
from src.shared.models.ledger import ACCOUNT_PLATFORM_CLEARING, ACCOUNT_STRIPE_FEES
from src.shared.models.order import Order
from src.shared.models.payout import PAYOUT_BLOCKED, PAYOUT_RELEASED, Payout
from src.shared.models.refund import REFUND_FAILED, REFUND_SUCCEEDED, Refund
from src.shared.utils.app_error import AppError
from tests.factories import make_order, make_payout, make_piece, make_user
from tests.helpers import assert_ledger_balanced, platform_balance

FEE = 320


def _paid_order(db, *, status="paid", payout_status=None):
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    piece = make_piece(db, seller, status="sold")
    order = make_order(
        db, buyer=buyer, seller=seller, piece=piece, status=status,
        payment_reference="pi_paid_1", stripe_charge_id="ch_paid_1",
    )
    money.book_order_paid(db, order, stripe_fee_cents=FEE)
    make_payout(db, order, **({"status": payout_status} if payout_status else {}))
    return order


def _admin(db):
    return make_user(db, is_admin=True)


def _payout(db, order) -> Payout:
    db.expire_all()
    return db.execute(select(Payout).where(Payout.order_id == order.id)).scalar_one()


def test_a_refund_leaves_the_stripe_fee_as_a_cost(db, stripe_enabled):
    """BE-1. Stripe keeps its fee on a refund, so after a full refund the Stripe balance is
    down by exactly that fee and the fee stays an expense. The old posting credited the fee
    back, so the books showed a balance a fee higher than Stripe's."""
    order = _paid_order(db)

    disputes_service.refund_order(order.id, _admin(db).id, "damaged in transit")

    # credits − debits: clearing has paid out a fee more than it took in; the fee is a cost.
    assert platform_balance(db, ACCOUNT_PLATFORM_CLEARING) == FEE
    assert platform_balance(db, ACCOUNT_STRIPE_FEES) == -FEE
    assert_ledger_balanced(db)
    assert [r["charge"] for r in stripe_enabled.refunds] == ["ch_paid_1"]


def test_a_failed_stripe_refund_holds_the_payout_and_can_be_retried(db, stripe_enabled):
    order = _paid_order(db)
    admin = _admin(db)
    stripe_enabled.refund_error = RuntimeError("Stripe timed out")

    with pytest.raises(AppError) as failed:
        disputes_service.refund_order(order.id, admin.id, "not as described")
    assert failed.value.status_code == 502

    db.expire_all()
    assert db.get(Order, order.id).status == "paid", "the order follows the money, not the attempt"
    assert _payout(db, order).status == PAYOUT_BLOCKED
    refund = db.execute(select(Refund).where(Refund.order_id == order.id)).scalar_one()
    assert refund.status == REFUND_FAILED

    disputes_service.refund_order(order.id, admin.id, "not as described")

    db.expire_all()
    assert db.get(Order, order.id).status == "refunded"
    assert db.get(Refund, refund.id).status == REFUND_SUCCEEDED
    assert len(stripe_enabled.refunds) == 1


def test_a_payout_cannot_go_out_while_a_refund_is_in_flight(db, stripe_enabled):
    """PAY-4. The refund used to drop its lock before calling Stripe, so a buyer confirming
    receipt in that gap released the artist's payout and the buyer was refunded as well."""
    order = _paid_order(db, status="awaiting_confirmation")
    stripe_enabled.refund_error = RuntimeError("Stripe timed out")
    with pytest.raises(AppError):
        disputes_service.refund_order(order.id, _admin(db).id, "never arrived")

    # The buyer confirms receipt while the refund is unresolved.
    db.expire_all()
    db.get(Order, order.id).status = "completed"
    db.commit()
    result = payouts_service.release_payout_for_order(order.id)

    assert result["status"] != PAYOUT_RELEASED
    assert stripe_enabled.transfers == []


def test_a_refund_stripe_already_made_counts_as_done(db, stripe_enabled):
    order = _paid_order(db)
    stripe_enabled.refund_error = stripe_enabled.error.InvalidRequestError(
        "Charge ch_paid_1 has already been refunded.", param=None, code="charge_already_refunded"
    )

    disputes_service.refund_order(order.id, _admin(db).id, "duplicate")

    db.expire_all()
    assert db.get(Order, order.id).status == "refunded"


def test_a_released_payout_stops_the_refund_before_stripe(db, stripe_enabled):
    order = _paid_order(db, status="awaiting_confirmation", payout_status=PAYOUT_RELEASED)

    with pytest.raises(AppError) as refused:
        disputes_service.refund_order(order.id, _admin(db).id, "too late")

    assert refused.value.status_code == 409
    assert stripe_enabled.refunds == []


@pytest.mark.parametrize("status", ["shipped", "awaiting_confirmation"])
def test_an_order_in_transit_or_delivered_can_be_refunded(db, stripe_enabled, status):
    """Stripe refunded these, then the state machine refused the order update — the buyer had
    their money back with the order still shipped, nothing in the ledger, the payout live."""
    order = _paid_order(db, status=status)

    disputes_service.refund_order(order.id, _admin(db).id, "lost in transit")

    db.expire_all()
    assert db.get(Order, order.id).status == "refunded"
    assert _payout(db, order).status == PAYOUT_BLOCKED
    assert_ledger_balanced(db)
