"""Auction checkout over HTTP — the winner pays the remainder, never the whole thing again.

An auction takes the hammer price when it closes, by capturing the winner's hold. Checkout
then exists only to collect shipping and tax and to record where the piece is going. Every
test here is about that split holding.
"""
import uuid
from datetime import datetime, timedelta, timezone

from src.modules.bids import auction_closer
from src.shared.models.auction import Auction
from src.shared.models.order import Order
from tests.factories import make_address, make_auction, make_bid, make_piece, make_user


def _won_auction(db, *, hammer_cents=500_00):
    """A closed auction settled on a winner, exactly as the sweep leaves it."""
    seller = make_user(db, seller=True)
    winner = make_user(db)
    piece = make_piece(db, seller, price_cents=400_00, status="live", listing_type="auction")
    now = datetime.now(timezone.utc)
    auction = make_auction(
        db, piece, seller,
        starting_bid_cents=400_00,
        opens_at=now - timedelta(days=7),
        closes_at=now - timedelta(minutes=1),
    )
    make_bid(db, auction, winner, hammer_cents)
    auction_closer.close_expired_auctions()
    db.expire_all()
    return seller, winner, piece, db.get(Auction, auction.id)


def test_the_winner_is_not_charged_the_hammer_price_twice(db, client, auth_headers):
    """The defect this endpoint was built wrong around.

    Closing the auction captured $500 from the winner's card. Checkout then created an order
    for $500 + shipping + tax and raised a payment intent for the whole total — so the
    artwork was taken a second time. On a five-thousand-dollar piece that is $5,000 of
    someone else's money.
    """
    seller, winner, piece, auction = _won_auction(db, hammer_cents=500_00)
    address = make_address(db, winner)

    response = client.post(
        f"/api/pieces/{piece.id}/auction-checkout",
        json={"addressId": str(address.id), "shippingMethod": "standard"},
        headers=auth_headers(winner),
    )

    assert response.status_code == 201, response.get_json()
    body = response.get_json()["data"]
    order = db.query(Order).filter_by(buyer_id=winner.id).one()

    assert order.prepaid_cents == 500_00, "checkout forgot the hammer price was already paid"
    assert order.artwork_cents == 500_00
    # What is left to collect is shipping and tax, and nothing else.
    assert body["balanceDueCents"] == order.total_cents - 500_00
    assert body["balanceDueCents"] == order.shipping_cents + order.tax_cents
    assert order.total_cents > order.prepaid_cents


def test_the_payment_intent_is_raised_on_the_balance_only(
    db, client, auth_headers, stripe_enabled
):
    seller, winner, piece, auction = _won_auction(db, hammer_cents=500_00)
    address = make_address(db, winner)
    created = client.post(
        f"/api/pieces/{piece.id}/auction-checkout",
        json={"addressId": str(address.id), "shippingMethod": "standard"},
        headers=auth_headers(winner),
    )
    order_id = created.get_json()["data"]["id"]

    response = client.post(
        f"/api/orders/{order_id}/create-payment-intent", headers=auth_headers(winner)
    )

    assert response.status_code in (200, 201), response.get_json()
    db.expire_all()
    order = db.get(Order, uuid.UUID(order_id))
    amount = response.get_json()["data"]["amountCents"]
    assert amount == order.total_cents - order.prepaid_cents
    assert amount < order.total_cents, "the intent still covers the already-captured artwork"


def test_a_losing_bidder_cannot_check_out(db, client, auth_headers):
    seller, winner, piece, auction = _won_auction(db)
    interloper = make_user(db)
    address = make_address(db, interloper)

    response = client.post(
        f"/api/pieces/{piece.id}/auction-checkout",
        json={"addressId": str(address.id), "shippingMethod": "standard"},
        headers=auth_headers(interloper),
    )

    assert response.status_code == 403


def test_checkout_is_refused_while_the_winner_still_owes_the_hammer_price(
    db, client, auth_headers, stripe_enabled
):
    """awaiting_payment means the close could not take their money. Letting them check out
    would create an order for a sale nobody has paid for."""
    seller = make_user(db, seller=True)
    winner = make_user(db)
    piece = make_piece(db, seller, price_cents=400_00, status="live", listing_type="auction")
    now = datetime.now(timezone.utc)
    auction = make_auction(
        db, piece, seller, starting_bid_cents=400_00,
        opens_at=now - timedelta(days=7), closes_at=now - timedelta(minutes=1),
    )
    bid = make_bid(db, auction, winner, 500_00)
    from src.shared.models.auction import Hold

    stripe_enabled.fail_capture(
        db.query(Hold).filter_by(bid_id=bid.id).one().stripe_payment_intent_id
    )
    auction_closer.close_expired_auctions()
    db.expire_all()
    address = make_address(db, winner)

    response = client.post(
        f"/api/pieces/{piece.id}/auction-checkout",
        json={"addressId": str(address.id), "shippingMethod": "standard"},
        headers=auth_headers(winner),
    )

    assert response.status_code == 409
    assert db.query(Order).filter_by(buyer_id=winner.id).count() == 0


def test_checking_out_twice_creates_one_order(db, client, auth_headers):
    seller, winner, piece, auction = _won_auction(db)
    address = make_address(db, winner)
    payload = {"addressId": str(address.id), "shippingMethod": "standard"}

    first = client.post(
        f"/api/pieces/{piece.id}/auction-checkout", json=payload, headers=auth_headers(winner)
    )
    second = client.post(
        f"/api/pieces/{piece.id}/auction-checkout", json=payload, headers=auth_headers(winner)
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert db.query(Order).filter_by(buyer_id=winner.id).count() == 1
