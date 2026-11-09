"""PAY-1 and PAY-2: who may cancel an order, and money that arrives after one is cancelled.

Rules decided 9 Nov: an unpaid order may be cancelled by either side, once Stripe agrees
nothing was charged. A paid order is cancelled by the artist (or an admin), with a full
refund — never by the buyer, whose route is to report an issue.
"""
from sqlalchemy import select

from src.modules.payments import money
from src.shared.models.ledger import ACCOUNT_PLATFORM_CLEARING, ACCOUNT_STRIPE_FEES
from src.shared.models.order import Order
from src.shared.models.payout import PAYOUT_BLOCKED, Payout
from tests.factories import make_order, make_payout, make_piece, make_user
from tests.helpers import assert_ledger_balanced, platform_balance
from tests.stripe_fixtures import make_event, post_webhook

FEE = 320


def _order(db, status, **kw):
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    piece = make_piece(db, seller, status="sold" if status != "pending_payment" else "reserved")
    order = make_order(
        db, buyer=buyer, seller=seller, piece=piece, status=status,
        payment_reference="pi_order_1", **kw,
    )
    return order, buyer, seller


def _cancel(client, auth_headers, order, who):
    return client.patch(
        f"/api/orders/{order.id}", json={"status": "cancelled"}, headers=auth_headers(who)
    )


def test_the_artist_can_cancel_a_paid_order_and_the_buyer_is_refunded(
    db, client, auth_headers, stripe_enabled
):
    order, _, seller = _order(db, "paid", stripe_charge_id="ch_order_1")
    money.book_order_paid(db, order, stripe_fee_cents=FEE)
    make_payout(db, order)

    response = _cancel(client, auth_headers, order, seller)

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["status"] == "refunded"
    db.expire_all()
    payout = db.execute(select(Payout).where(Payout.order_id == order.id)).scalar_one()
    assert payout.status == PAYOUT_BLOCKED
    assert len(stripe_enabled.refunds) == 1
    assert_ledger_balanced(db)


def test_the_buyer_cannot_cancel_a_paid_order(db, client, auth_headers, stripe_enabled):
    order, buyer, _ = _order(db, "paid", stripe_charge_id="ch_order_1")

    response = _cancel(client, auth_headers, order, buyer)

    assert response.status_code == 403
    assert "report an issue" in response.get_json()["message"]
    assert stripe_enabled.refunds == []


def test_the_artist_cannot_cancel_once_it_has_shipped(db, client, auth_headers, stripe_enabled):
    order, _, seller = _order(db, "shipped", stripe_charge_id="ch_order_1")

    assert _cancel(client, auth_headers, order, seller).status_code == 409
    assert stripe_enabled.refunds == []


def test_an_unpaid_order_is_cancelled_only_once_stripe_stops_the_payment(
    db, client, auth_headers, stripe_enabled
):
    order, buyer, _ = _order(db, "pending_payment")

    response = _cancel(client, auth_headers, order, buyer)

    assert response.status_code == 200
    assert stripe_enabled.cancelled_intents == ["pi_order_1"]
    db.expire_all()
    assert db.get(Order, order.id).status == "cancelled"


def test_an_unpaid_order_mid_payment_is_not_cancelled(
    db, client, auth_headers, stripe_enabled, monkeypatch
):
    order, buyer, _ = _order(db, "pending_payment")

    def processing(intent_id, **kwargs):
        raise stripe_enabled.error.InvalidRequestError(
            "You cannot cancel this PaymentIntent because it has a status of processing.",
            param=None,
        )

    monkeypatch.setattr(stripe_enabled.PaymentIntent, "cancel", processing)

    assert _cancel(client, auth_headers, order, buyer).status_code == 409
    db.expire_all()
    assert db.get(Order, order.id).status == "pending_payment"


def _late_payment(order, event_id):
    return make_event(
        "payment_intent.succeeded",
        {
            "id": "pi_order_1",
            "amount": order.total_cents,
            "amount_received": order.total_cents,
            "latest_charge": "ch_late_1",
            "metadata": {"order_id": str(order.id)},
        },
        event_id=event_id,
    )


def test_money_arriving_for_a_cancelled_order_goes_straight_back(db, client, stripe_enabled):
    """PAY-2. This used to be logged and dropped: the buyer was charged for nothing."""
    order, _, _ = _order(db, "cancelled")
    stripe_enabled.add_charge("ch_late_1", fee_cents=FEE, amount_cents=order.total_cents)

    assert post_webhook(client, _late_payment(order, "evt_late_1")).status_code == 200
    # Stripe can deliver the same payment twice under different event ids.
    assert post_webhook(client, _late_payment(order, "evt_late_2")).status_code == 200

    assert [r["payment_intent"] for r in stripe_enabled.refunds] == ["pi_order_1"]
    db.expire_all()
    assert db.get(Order, order.id).status == "cancelled"
    assert platform_balance(db, ACCOUNT_STRIPE_FEES) == -FEE
    assert platform_balance(db, ACCOUNT_PLATFORM_CLEARING) == FEE
    assert_ledger_balanced(db)


def test_a_late_refund_that_fails_is_retried_by_stripe(db, client, stripe_enabled):
    order, _, _ = _order(db, "cancelled")
    stripe_enabled.add_charge("ch_late_1", fee_cents=FEE, amount_cents=order.total_cents)
    stripe_enabled.refund_error = RuntimeError("Stripe timed out")
    event = _late_payment(order, "evt_late_1")

    assert post_webhook(client, event).status_code == 500
    assert post_webhook(client, event).status_code == 200

    assert len(stripe_enabled.refunds) == 1
    assert_ledger_balanced(db)
