"""The auction actions a person takes, over HTTP, plus the ledger they add up to.

Seller: extend, cancel, relist. Winner: retry a declined payment. Each one moves other
people's money, so each test asserts where that money ended up rather than only the status
code.
"""
import uuid
from datetime import datetime, timedelta, timezone

from src.modules.bids import auction_closer
from src.shared.models.auction import (
    AUCTION_AWAITING_WINNER,
    AUCTION_CANCELLED,
    AUCTION_LIVE,
    AUCTION_CLOSED_NO_BIDS,
    AUCTION_NEEDS_SELLER_ACTION,
    HOLD_CAPTURED,
    HOLD_RELEASED,
    Auction,
    Hold,
)
from src.shared.models.bid import BID_CANCELLED, Bid
from src.shared.models.piece import Piece
from tests.factories import make_auction, make_bid, make_piece, make_user
from tests.helpers import assert_ledger_balanced


def _live_auction(db, *, days_left=7):
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=300_00, status="live", listing_type="auction")
    now = datetime.now(timezone.utc)
    auction = make_auction(
        db, piece, seller,
        starting_bid_cents=300_00,
        opens_at=now - timedelta(days=1),
        closes_at=now + timedelta(days=days_left),
    )
    return seller, piece, auction


def _hold(db, bid) -> Hold:
    return db.query(Hold).filter_by(bid_id=bid.id).one()


# --- extend -------------------------------------------------------------------------------

def test_a_seller_can_extend_once_and_bids_survive(db, client, auth_headers):
    seller, piece, auction = _live_auction(db, days_left=7)
    bidder = make_user(db)
    bid = make_bid(db, auction, bidder, 350_00)
    original_close = auction.closes_at

    response = client.post(
        f"/api/pieces/{piece.id}/auction/extend",
        json={"extraDays": 2},
        headers=auth_headers(seller),
    )

    assert response.status_code == 200, response.get_json()
    db.expire_all()
    auction = db.get(Auction, auction.id)
    assert auction.closes_at > original_close
    assert auction.extended_once is True
    # The bidder's position and money are untouched — an extension is not a restart.
    assert db.get(Bid, bid.id).status == "active"
    assert _hold(db, bid).status != HOLD_RELEASED


def test_extending_twice_is_refused(db, client, auth_headers):
    seller, piece, auction = _live_auction(db, days_left=7)
    payload = {"extraDays": 1}

    first = client.post(
        f"/api/pieces/{piece.id}/auction/extend", json=payload, headers=auth_headers(seller)
    )
    second = client.post(
        f"/api/pieces/{piece.id}/auction/extend", json=payload, headers=auth_headers(seller)
    )

    assert first.status_code == 200
    assert second.status_code == 409


def test_extending_close_to_the_end_is_refused(db, client, auth_headers):
    """Otherwise extending becomes a way to control exactly when an auction ends, which is
    the manipulation the rule exists to prevent."""
    seller, piece, auction = _live_auction(db, days_left=1)

    response = client.post(
        f"/api/pieces/{piece.id}/auction/extend",
        json={"extraDays": 2},
        headers=auth_headers(seller),
    )

    assert response.status_code == 409


def test_a_stranger_cannot_extend_someone_elses_auction(db, client, auth_headers):
    seller, piece, auction = _live_auction(db)
    stranger = make_user(db)

    response = client.post(
        f"/api/pieces/{piece.id}/auction/extend",
        json={"extraDays": 1},
        headers=auth_headers(stranger),
    )

    assert response.status_code == 404


# --- cancel -------------------------------------------------------------------------------

def test_cancelling_releases_every_bidder(db, client, auth_headers):
    seller, piece, auction = _live_auction(db)
    a, b = make_user(db), make_user(db)
    bid_a = make_bid(db, auction, a, 350_00)
    bid_b = make_bid(db, auction, b, 400_00)

    response = client.post(
        f"/api/pieces/{piece.id}/auction/cancel", headers=auth_headers(seller)
    )

    assert response.status_code == 200, response.get_json()
    db.expire_all()
    assert db.get(Auction, auction.id).status == AUCTION_CANCELLED
    for bid in (bid_a, bid_b):
        assert db.get(Bid, bid.id).status == BID_CANCELLED
        assert _hold(db, bid).status == HOLD_RELEASED, "a cancelled auction kept someone's money"
    assert db.get(Piece, piece.id).status == "delisted"


# --- relist -------------------------------------------------------------------------------

def test_relisting_starts_a_new_auction_and_keeps_the_old_one_as_history(
    db, client, auth_headers
):
    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece = make_piece(db, seller, price_cents=300_00, status="live", listing_type="auction")
    now = datetime.now(timezone.utc)
    first = make_auction(
        db, piece, seller, starting_bid_cents=300_00, reserve_cents=900_00,
        opens_at=now - timedelta(days=7), closes_at=now - timedelta(minutes=1),
    )
    make_bid(db, first, bidder, 400_00)
    # Reserve not met, so the sweep parks it for the seller.
    auction_closer.close_expired_auctions()
    db.expire_all()
    assert db.get(Auction, first.id).status == AUCTION_NEEDS_SELLER_ACTION

    response = client.post(
        f"/api/pieces/{piece.id}/auction/relist",
        json={"durationDays": 5, "startingBidCents": 250_00},
        headers=auth_headers(seller),
    )

    assert response.status_code == 201, response.get_json()
    db.expire_all()
    auctions = db.query(Auction).filter_by(piece_id=piece.id).all()
    assert len(auctions) == 2, "relisting overwrote the record of what happened the first time"
    assert db.get(Auction, first.id).status == AUCTION_NEEDS_SELLER_ACTION
    fresh = next(a for a in auctions if a.id != first.id)
    assert fresh.status == AUCTION_LIVE
    assert fresh.starting_bid_cents == 250_00
    assert fresh.closes_at > datetime.now(timezone.utc)
    assert db.get(Piece, piece.id).status == "live"


def test_an_auction_nobody_bid_on_can_be_relisted(db, client, auth_headers):
    """The commonest way an auction fails, and the one relisting refused.

    It only accepted needs_seller_action — the reserve-not-met case. An auction that simply
    got no bids left the work delisted with nothing in the product offering to try again,
    which for a marketplace is the wrong place to stop: work that does not sell the first
    time is usually priced wrong, not unsellable.
    """
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=300_00, status="live", listing_type="auction")
    now = datetime.now(timezone.utc)
    first = make_auction(
        db, piece, seller, starting_bid_cents=300_00,
        opens_at=now - timedelta(days=7), closes_at=now - timedelta(minutes=1),
    )
    auction_closer.close_expired_auctions()
    db.expire_all()
    assert db.get(Auction, first.id).status == AUCTION_CLOSED_NO_BIDS
    assert db.get(Piece, piece.id).status == "delisted", "an unsold auction delists the work"

    response = client.post(
        f"/api/pieces/{piece.id}/auction/relist",
        json={"durationDays": 5, "startingBidCents": 150_00},
        headers=auth_headers(seller),
    )

    assert response.status_code == 201, response.get_json()
    db.expire_all()
    assert db.get(Piece, piece.id).status == "live", "the work is back on the market"
    fresh = next(a for a in db.query(Auction).filter_by(piece_id=piece.id).all()
                 if a.id != first.id)
    assert fresh.status == AUCTION_LIVE
    assert fresh.starting_bid_cents == 150_00
    # The failed attempt stays exactly as it ended.
    assert db.get(Auction, first.id).status == AUCTION_CLOSED_NO_BIDS


def test_a_sold_auction_cannot_be_relisted(db, client, auth_headers):
    """It has an order and a buyer behind it. Relisting would offer work that has already
    changed hands."""
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=300_00, status="sold", listing_type="auction")
    now = datetime.now(timezone.utc)
    auction = make_auction(db, piece, seller, starting_bid_cents=300_00,
                           opens_at=now - timedelta(days=2), closes_at=now - timedelta(days=1))
    auction.status = "closed_sold"
    db.commit()

    response = client.post(
        f"/api/pieces/{piece.id}/auction/relist",
        json={"durationDays": 5}, headers=auth_headers(seller),
    )

    assert response.status_code == 409


def test_a_cancelled_auction_cannot_be_relisted(db, client, auth_headers):
    """Cancelling was somebody deciding to stop. Relisting from here would quietly undo a
    withdrawal that bidders were told about."""
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=300_00, status="delisted", listing_type="auction")
    now = datetime.now(timezone.utc)
    auction = make_auction(db, piece, seller, starting_bid_cents=300_00,
                           opens_at=now - timedelta(days=2), closes_at=now - timedelta(days=1))
    auction.status = "cancelled"
    db.commit()

    response = client.post(
        f"/api/pieces/{piece.id}/auction/relist",
        json={"durationDays": 5}, headers=auth_headers(seller),
    )

    assert response.status_code == 409


def test_a_piece_with_a_running_auction_cannot_be_relisted(db, client, auth_headers):
    seller, piece, auction = _live_auction(db)

    response = client.post(
        f"/api/pieces/{piece.id}/auction/relist",
        json={"durationDays": 5},
        headers=auth_headers(seller),
    )

    assert response.status_code == 409


# --- the winner retrying a declined payment ----------------------------------------------

def test_the_winner_can_fix_a_declined_card_inside_their_window(
    db, client, auth_headers, stripe_enabled
):
    seller = make_user(db, seller=True)
    winner = make_user(db)
    piece = make_piece(db, seller, price_cents=300_00, status="live", listing_type="auction")
    now = datetime.now(timezone.utc)
    auction = make_auction(
        db, piece, seller, starting_bid_cents=300_00,
        opens_at=now - timedelta(days=7), closes_at=now - timedelta(minutes=1),
    )
    bid = make_bid(db, auction, winner, 400_00)
    stripe_enabled.fail_capture(_hold(db, bid).stripe_payment_intent_id)
    auction_closer.close_expired_auctions()
    db.expire_all()

    response = client.post(
        f"/api/pieces/{piece.id}/auction/retry-payment",
        json={"paymentMethodId": "pm_test_another_card"},
        headers=auth_headers(winner),
    )

    assert response.status_code == 200, response.get_json()
    db.expire_all()
    assert db.get(Auction, auction.id).status == AUCTION_AWAITING_WINNER
    assert _hold(db, bid).status == HOLD_CAPTURED
    assert_ledger_balanced(db)


def test_only_the_winner_can_retry_the_payment(db, client, auth_headers, stripe_enabled):
    seller = make_user(db, seller=True)
    winner, other = make_user(db), make_user(db)
    piece = make_piece(db, seller, price_cents=300_00, status="live", listing_type="auction")
    now = datetime.now(timezone.utc)
    auction = make_auction(
        db, piece, seller, starting_bid_cents=300_00,
        opens_at=now - timedelta(days=7), closes_at=now - timedelta(minutes=1),
    )
    bid = make_bid(db, auction, winner, 400_00)
    stripe_enabled.fail_capture(_hold(db, bid).stripe_payment_intent_id)
    auction_closer.close_expired_auctions()
    db.expire_all()

    response = client.post(
        f"/api/pieces/{piece.id}/auction/retry-payment",
        json={"paymentMethodId": "pm_test_another_card"},
        headers=auth_headers(other),
    )

    assert response.status_code == 403


# --- the whole money path ------------------------------------------------------------------

def test_a_won_auction_books_one_balanced_sale_across_both_charges(
    db, client, auth_headers, stripe_enabled
):
    """End to end: the close captures the hammer price into escrow, checkout collects the
    balance, and the webhook books one `order_paid` that clears escrow rather than debiting
    the artwork into the platform's assets a second time.
    """
    from src.modules.payments import money
    from src.shared.ledger.ledger_service import get_seller_balance
    from src.shared.models.order import Order
    from tests.factories import make_address

    seller = make_user(db, seller=True)
    winner = make_user(db)
    piece = make_piece(db, seller, price_cents=400_00, status="live", listing_type="auction")
    now = datetime.now(timezone.utc)
    auction = make_auction(
        db, piece, seller, starting_bid_cents=400_00,
        opens_at=now - timedelta(days=7), closes_at=now - timedelta(minutes=1),
    )
    make_bid(db, auction, winner, 500_00)
    auction_closer.close_expired_auctions()
    db.expire_all()
    assert_ledger_balanced(db)

    address = make_address(db, winner)
    created = client.post(
        f"/api/pieces/{piece.id}/auction-checkout",
        json={"addressId": str(address.id), "shippingMethod": "standard"},
        headers=auth_headers(winner),
    )
    order = db.get(Order, uuid.UUID(created.get_json()["data"]["id"]))

    money.book_order_paid(db, order, stripe_fee_cents=0)

    db.expire_all()
    assert_ledger_balanced(db)
    # The artist is owed their share of the hammer price once, not twice.
    expected = money.artist_share_cents(500_00, order.commission_bps)
    assert get_seller_balance(db, seller.id) == expected


def test_refunding_an_auction_order_gives_back_both_charges(
    db, client, auth_headers, stripe_enabled
):
    """An auction order is paid in two goes, so a refund has to reverse two charges.

    book_refund_issued reverses the full total. Refunding only the balance charge would have
    the ledger claim a refund that never happened and leave the buyer out the entire hammer
    price — the single largest number in the transaction.
    """
    from src.modules.admin import disputes_service
    from src.modules.payments import money
    from src.shared.models.order import Order
    from tests.factories import make_address

    seller = make_user(db, seller=True)
    winner = make_user(db)
    admin = make_user(db, is_admin=True)
    piece = make_piece(db, seller, price_cents=400_00, status="live", listing_type="auction")
    now = datetime.now(timezone.utc)
    auction = make_auction(
        db, piece, seller, starting_bid_cents=400_00,
        opens_at=now - timedelta(days=7), closes_at=now - timedelta(minutes=1),
    )
    make_bid(db, auction, winner, 500_00)
    auction_closer.close_expired_auctions()
    db.expire_all()

    address = make_address(db, winner)
    created = client.post(
        f"/api/pieces/{piece.id}/auction-checkout",
        json={"addressId": str(address.id), "shippingMethod": "standard"},
        headers=auth_headers(winner),
    )
    order = db.get(Order, uuid.UUID(created.get_json()["data"]["id"]))
    balance_charge = stripe_enabled.add_charge("ch_balance", fee_cents=0, amount_cents=0)
    order.stripe_charge_id = balance_charge["id"]
    from src.modules.orders import orders_dao

    orders_dao.transition_order(db, order, "paid", commit=False)
    money.book_order_paid(db, order, stripe_fee_cents=0, commit=False)
    db.commit()

    disputes_service.refund_order(order.id, admin.id, reason="test")

    # Two refunds: the shipping-and-tax charge, and the captured hammer price.
    assert len(stripe_enabled.refunds) == 2, (
        "the hammer price was never given back — the ledger says refunded, Stripe does not"
    )
    assert any(r.get("charge") == "ch_balance" for r in stripe_enabled.refunds)
    db.expire_all()
    assert any(
        r.get("payment_intent") == db.get(Order, order.id).prepaid_reference
        for r in stripe_enabled.refunds
    )
    assert_ledger_balanced(db)
