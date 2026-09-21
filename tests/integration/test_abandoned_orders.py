"""Putting artwork back on sale after a checkout is abandoned.

Creating an order reserves its piece before any money moves. Every release of that
reservation is triggered by a Stripe webhook — and a collector who simply closes the payment
sheet produces no webhook at all. Without this sweep the order sits `pending_payment` and the
piece stays `reserved` permanently: the artist cannot relist it and nobody else can buy it.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.modules.orders import stale_orders
from src.modules.pieces import piece_state
from src.shared.models.order import Order
from src.shared.models.piece import Piece
from tests.factories import make_order, make_piece, make_user


def _abandoned_checkout(db, *, minutes_ago: int = 30, reference: str | None = "pi_stale_1"):
    seller, buyer = make_user(db, seller=True), make_user(db)
    piece = make_piece(db, seller, price_cents=300_00, status="live")
    order = make_order(db, buyer=buyer, seller=seller, piece=piece,
                       artwork_cents=300_00, payment_reference=reference)
    piece_state.transition_piece(db, piece, piece_state.RESERVED,
                                 allowed_from={piece_state.LIVE})
    order.created_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    db.commit()
    return order, piece


def test_an_abandoned_checkout_puts_the_piece_back_on_sale(db, stripe_enabled):
    order, piece = _abandoned_checkout(db)

    assert stale_orders.expire_abandoned_orders() == 1

    db.expire_all()
    assert db.get(Order, order.id).status == "cancelled"
    assert db.get(Piece, piece.id).status == piece_state.LIVE


def test_the_intent_is_cancelled_so_a_late_client_cannot_still_pay(db, stripe_enabled):
    """Releasing the piece without closing the intent would leave a client able to confirm
    it later — a real charge against an order we cancelled, for work now sold to someone
    else."""
    order, _ = _abandoned_checkout(db)

    stale_orders.expire_abandoned_orders()

    assert "pi_stale_1" in stripe_enabled.cancelled_intents


def test_a_collector_still_at_the_card_screen_is_left_alone(db, stripe_enabled):
    """The window is a floor, not a deadline: nothing inside it is touched."""
    order, piece = _abandoned_checkout(db, minutes_ago=2)

    assert stale_orders.expire_abandoned_orders() == 0

    db.expire_all()
    assert db.get(Order, order.id).status == "pending_payment"
    assert db.get(Piece, piece.id).status == piece_state.RESERVED


def test_an_order_stripe_refuses_to_cancel_is_never_released(db, stripe_enabled, monkeypatch):
    """The dangerous case. Stripe rejects cancelling an intent that is processing or already
    succeeded — meaning the money is in flight. Releasing on our own clock would hand the
    piece to someone else while the first collector's card is being charged.
    """
    order, piece = _abandoned_checkout(db)

    def _refuse(intent_id, **kwargs):
        raise RuntimeError("You cannot cancel this PaymentIntent because it has a status of "
                           "processing.")

    monkeypatch.setattr(stripe_enabled.PaymentIntent, "cancel", _refuse)

    assert stale_orders.expire_abandoned_orders() == 0

    db.expire_all()
    assert db.get(Order, order.id).status == "pending_payment"
    assert db.get(Piece, piece.id).status == piece_state.RESERVED


def test_a_network_failure_leaves_the_order_for_the_next_pass(db, stripe_enabled, monkeypatch):
    """Being wrong by waiting costs a few more minutes. Being wrong by releasing costs a
    double sale, so every unexpected error resolves the same way."""
    order, _ = _abandoned_checkout(db)
    monkeypatch.setattr(
        stripe_enabled.PaymentIntent, "cancel",
        lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("timed out")),
    )

    assert stale_orders.expire_abandoned_orders() == 0
    db.expire_all()
    assert db.get(Order, order.id).status == "pending_payment"


def test_an_order_that_never_reached_stripe_is_still_released(db, stripe_enabled):
    """Checkout can fail before an intent exists. There is nothing that could have been
    paid, so the piece must not stay reserved on account of it."""
    order, piece = _abandoned_checkout(db, reference=None)

    assert stale_orders.expire_abandoned_orders() == 1

    db.expire_all()
    assert db.get(Piece, piece.id).status == piece_state.LIVE


@pytest.mark.parametrize("status", ["paid", "cancelled", "refunded", "completed"])
def test_only_unpaid_orders_are_considered(db, stripe_enabled, status):
    """A paid order's piece is reserved on purpose, and stays that way."""
    order, piece = _abandoned_checkout(db)
    order.status = status
    db.commit()

    assert stale_orders.expire_abandoned_orders() == 0

    db.expire_all()
    assert db.get(Order, order.id).status == status


def test_the_sweep_is_safe_to_run_twice(db, stripe_enabled):
    """Celery's acks_late means redelivery is normal. A second pass must not re-cancel an
    order or disturb a piece somebody has since bought."""
    order, piece = _abandoned_checkout(db)

    first = stale_orders.expire_abandoned_orders()
    second = stale_orders.expire_abandoned_orders()

    assert (first, second) == (1, 0)
    db.expire_all()
    assert db.get(Piece, piece.id).status == piece_state.LIVE


def test_one_stuck_order_does_not_block_the_rest(db, stripe_enabled, monkeypatch):
    """A sweep that gives up on the first awkward order would leave every piece behind it
    reserved — the exact failure it exists to prevent."""
    stuck, stuck_piece = _abandoned_checkout(db, reference="pi_stuck")
    fine, fine_piece = _abandoned_checkout(db, reference="pi_fine")

    real_cancel = stripe_enabled.PaymentIntent.cancel

    def _selective(intent_id, **kwargs):
        if intent_id == "pi_stuck":
            raise RuntimeError("status of processing")
        return real_cancel(intent_id, **kwargs)

    monkeypatch.setattr(stripe_enabled.PaymentIntent, "cancel", _selective)

    assert stale_orders.expire_abandoned_orders() == 1

    db.expire_all()
    assert db.get(Piece, stuck_piece.id).status == piece_state.RESERVED
    assert db.get(Piece, fine_piece.id).status == piece_state.LIVE
