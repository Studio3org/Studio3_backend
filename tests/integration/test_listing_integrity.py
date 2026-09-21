"""Fixed-vs-auction invariants, end to end through the API where it matters.

The headline case: `POST /pieces/<id>/collect` on a live auction. The Flutter client hides
the button, but the endpoint is authenticated and public, so client-side gating was the only
thing standing between a $5,000 auction and someone buying it for the $200 opening bid.
"""
import pytest

from src.modules.pieces import listing_rules, piece_state
from src.shared.config.auction_config import bid_increment_cents
from src.shared.models.bid import BID_ACTIVE, BID_CANCELLED
from src.shared.models.piece import Piece
from src.shared.utils.app_error import AppError
from tests.factories import make_address, make_auction, make_bid, make_piece, make_user


def _auction(db, seller, *, price_cents=200_00, **kwargs):
    """A piece listed for auction, with its auction row. Returns the piece.

    Auction state lives on the Auction now, so a piece with listing_type='auction' and no
    auction row is a state the application never produces.
    """
    piece = make_piece(db, seller, price_cents=price_cents, listing_type="auction")
    make_auction(db, piece, seller, starting_bid_cents=price_cents, **kwargs)
    return piece


def _auction_row(db, piece):
    from src.modules.bids import auction_dao

    return auction_dao.get_running_auction(db, piece.id)


# --- the collect() hole ----------------------------------------------------------------

def test_collect_refuses_an_auction_piece(db, client, auth_headers):
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    piece = _auction(db, seller, price_cents=200_00)
    address = make_address(db, buyer)

    response = client.post(
        f"/api/pieces/{piece.id}/collect",
        json={"addressId": str(address.id), "shippingMethod": "standard"},
        headers=auth_headers(buyer),
    )

    assert response.status_code == 409
    assert "auction" in response.get_json()["message"].lower()
    db.expire_all()
    assert db.get(Piece, piece.id).status == "live", "auction piece was reserved by a purchase"


def test_collect_still_works_for_a_fixed_price_piece(db, client, auth_headers):
    seller = make_user(db, seller=True)
    buyer = make_user(db)
    piece = make_piece(db, seller, price_cents=200_00, listing_type="fixed")
    address = make_address(db, buyer)

    response = client.post(
        f"/api/pieces/{piece.id}/collect",
        json={"addressId": str(address.id), "shippingMethod": "standard"},
        headers=auth_headers(buyer),
    )

    assert response.status_code == 201
    db.expire_all()
    assert db.get(Piece, piece.id).status == "reserved"


def test_bidding_refuses_a_fixed_price_piece(db, client, auth_headers):
    """The mirror image — the guard has to work in both directions."""
    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece = make_piece(db, seller, price_cents=200_00, listing_type="fixed")

    response = client.post(
        f"/api/pieces/{piece.id}/bids",
        json={"amountCents": 300_00},
        headers=auth_headers(bidder),
    )

    assert response.status_code == 400


# --- the artist's stated minimum is biddable -------------------------------------------

def test_first_bid_may_land_exactly_on_the_starting_bid(db, client, auth_headers):
    """Regression: min_next_bid was starting + increment, so the one number the artist
    advertised as their minimum was the one amount nobody could bid."""
    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece = _auction(db, seller, price_cents=200_00)

    response = client.post(
        f"/api/pieces/{piece.id}/bids",
        json={"amountCents": 200_00, "paymentMethodId": "pm_test_card"},
        headers=auth_headers(bidder),
    )

    assert response.status_code == 201
    body = response.get_json()["data"]
    assert body["amountCents"] == 200_00
    assert body["startingBidCents"] == 200_00
    assert body["highestBidCents"] == 200_00
    assert body["isHighestBidder"] is True, "bid response disagreed with GET detail"


def test_second_bid_must_clear_the_banded_increment(db, client, auth_headers):
    seller = make_user(db, seller=True)
    first, second = make_user(db), make_user(db)
    piece = _auction(db, seller, price_cents=900_00)
    make_bid(db, _auction_row(db, piece), first, 900_00)

    # $900 sits in the $500-1,999 band, so the step is $25.
    assert bid_increment_cents(900_00) == 25_00

    too_low = client.post(
        f"/api/pieces/{piece.id}/bids",
        json={"amountCents": 910_00, "paymentMethodId": "pm_test_card"},
        headers=auth_headers(second),
    )
    assert too_low.status_code == 409

    exact = client.post(
        f"/api/pieces/{piece.id}/bids",
        json={"amountCents": 925_00, "paymentMethodId": "pm_test_card"},
        headers=auth_headers(second),
    )
    assert exact.status_code == 201


def test_a_seller_cannot_bid_on_their_own_piece(db, client, auth_headers):
    seller = make_user(db, seller=True)
    piece = _auction(db, seller)
    response = client.post(
        f"/api/pieces/{piece.id}/bids",
        json={"amountCents": 300_00, "paymentMethodId": "pm_test_card"},
        headers=auth_headers(seller),
    )
    assert response.status_code == 400


# --- patch() can no longer set money-driven state --------------------------------------

@pytest.mark.parametrize("status", ["sold", "reserved", "auction_won", "deleted", "banana"])
def test_owner_cannot_set_a_money_driven_status(db, client, auth_headers, status):
    """Regression: `piece.status = body["status"]` had no whitelist, so an owner could hand
    themselves auction_won — the exact state auction_checkout trusts."""
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller)

    response = client.patch(
        f"/api/pieces/{piece.id}", json={"status": status}, headers=auth_headers(seller)
    )

    assert response.status_code == 403
    db.expire_all()
    assert db.get(Piece, piece.id).status == "live"


def test_owner_may_still_delist_and_relist(db, client, auth_headers):
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller)

    assert client.patch(
        f"/api/pieces/{piece.id}", json={"status": "delisted"}, headers=auth_headers(seller)
    ).status_code == 200
    assert client.patch(
        f"/api/pieces/{piece.id}", json={"status": "live"}, headers=auth_headers(seller)
    ).status_code == 200
    db.expire_all()
    assert db.get(Piece, piece.id).status == "live"


def test_invalid_listing_type_is_rejected(db, client, auth_headers):
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller)

    response = client.patch(
        f"/api/pieces/{piece.id}", json={"listingType": "banana"}, headers=auth_headers(seller)
    )

    assert response.status_code == 400
    db.expire_all()
    assert db.get(Piece, piece.id).listing_type == "fixed"


def test_terms_are_frozen_once_an_auction_has_bids(db, client, auth_headers):
    """Switching a live auction to fixed-price orphans every bid against it."""
    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece = _auction(db, seller, price_cents=200_00)
    make_bid(db, _auction_row(db, piece), bidder, 250_00)

    response = client.patch(
        f"/api/pieces/{piece.id}", json={"listingType": "fixed"}, headers=auth_headers(seller)
    )

    assert response.status_code == 409
    assert "bids" in response.get_json()["message"].lower()
    db.expire_all()
    assert db.get(Piece, piece.id).listing_type == "auction"


def test_descriptive_edits_still_allowed_on_an_auction_with_bids(db, client, auth_headers):
    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece = _auction(db, seller)
    make_bid(db, _auction_row(db, piece), bidder, 250_00)

    response = client.patch(
        f"/api/pieces/{piece.id}", json={"title": "Renamed"}, headers=auth_headers(seller)
    )

    assert response.status_code == 200
    db.expire_all()
    assert db.get(Piece, piece.id).title == "Renamed"


def test_terms_are_frozen_once_a_sale_is_in_flight(db, client, auth_headers):
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, status="reserved")

    response = client.patch(
        f"/api/pieces/{piece.id}", json={"priceCents": 50_00}, headers=auth_headers(seller)
    )

    assert response.status_code == 409


# --- the auction clock ------------------------------------------------------------------

def test_an_auctions_clock_belongs_to_the_auction_not_the_piece(db):
    """The stale-clock bug is now structurally impossible: timing lives on the Auction row,
    which is created fresh per run, so a relisted piece cannot inherit a past end time."""
    seller = make_user(db, seller=True)
    piece = _auction(db, seller)

    assert not hasattr(db.get(Piece, piece.id), "auction_ends_at")
    auction = _auction_row(db, piece)
    assert auction.closes_at is not None and auction.opens_at is not None
    assert auction.closes_at > auction.opens_at


def test_switching_an_auction_without_bids_to_fixed_is_allowed(db, client, auth_headers):
    seller = make_user(db, seller=True)
    piece = _auction(db, seller)

    response = client.patch(
        f"/api/pieces/{piece.id}",
        json={"listingType": "fixed", "priceCents": 300_00},
        headers=auth_headers(seller),
    )

    assert response.status_code == 200
    db.expire_all()
    assert db.get(Piece, piece.id).listing_type == "fixed"


# --- state machine invariants -----------------------------------------------------------

def test_delisting_a_live_auction_with_bids_is_refused(db):
    """It has to go through cancel_auction, which releases holds and tells the bidders."""
    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece = _auction(db, seller)
    make_bid(db, _auction_row(db, piece), bidder, 250_00)

    with pytest.raises(AppError, match="Cancel the auction"):
        piece_state.transition_piece(db, piece, piece_state.DELISTED)


def test_released_auction_returns_to_its_winner_not_to_live(db):
    """`live` would reopen bidding against an end time already in the past."""
    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece = _auction(db, seller)
    make_bid(db, _auction_row(db, piece), bidder, 250_00)

    assert piece_state.release_target_status(db, piece) == piece_state.AUCTION_WON

    fixed = make_piece(db, seller, listing_type="fixed")
    assert piece_state.release_target_status(db, fixed) == piece_state.LIVE


def test_cancelled_bids_are_not_counted_or_returned_as_highest(db):
    from src.modules.bids import bid_dao

    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece = _auction(db, seller)
    auction = _auction_row(db, piece)
    make_bid(db, auction, bidder, 250_00)

    assert bid_dao.count_active_bids(db, auction.id) == 1
    cancelled = bid_dao.cancel_active_bids(db, auction.id, reason="seller_cancelled")

    assert len(cancelled) == 1
    assert cancelled[0].status == BID_CANCELLED
    assert bid_dao.count_active_bids(db, auction.id) == 0
    assert bid_dao.get_highest_bid(db, auction.id) is None
    # Still there for the dispute record.
    assert len(bid_dao.list_bids_for_seller(db, auction.id)) == 1


def test_owner_driven_statuses_exclude_everything_money_touches(db):
    assert set(listing_rules.OWNER_DRIVEN_STATUSES).isdisjoint(piece_state.COMMITTED_STATUSES)
    assert piece_state.DELETED not in listing_rules.OWNER_DRIVEN_STATUSES
    assert BID_ACTIVE == "active"


# --- seller deactivation ----------------------------------------------------------------

def test_deactivating_a_seller_cancels_live_auctions_and_notifies_every_bidder(db):
    """Regression: this set is_for_sale=False and status='delisted' directly, which took the
    piece out of the close sweep's query. The auction never closed and nobody bidding on it
    was ever told anything — their money stayed committed to a sale that would never happen.
    """
    from sqlalchemy import select

    from src.modules.bids.auction_cancellation import REASON_SELLER_DEACTIVATED
    from src.modules.pieces import listing_service
    from src.shared.models.notification import Notification

    seller = make_user(db, seller=True)
    first, second = make_user(db), make_user(db)
    auction = _auction(db, seller, price_cents=200_00)
    fixed = make_piece(db, seller, price_cents=300_00, listing_type="fixed")
    make_bid(db, _auction_row(db, auction), first, 200_00)
    make_bid(db, _auction_row(db, auction), second, 250_00)

    result = listing_service.delist_all_for_seller(
        db, seller.id, reason=REASON_SELLER_DEACTIVATED
    )

    assert result["cancelledAuctions"] == 1
    assert result["delisted"] == 1

    db.expire_all()
    cancelled_piece = db.get(Piece, auction.id)
    assert cancelled_piece.status == piece_state.DELISTED
    from src.shared.models.auction import AUCTION_CANCELLED, Auction

    auction_row = db.query(Auction).filter_by(piece_id=auction.id).one()
    assert auction_row.status == AUCTION_CANCELLED, "the auction row was left running"
    assert db.get(Piece, fixed.id).status == piece_state.DELISTED
    assert db.get(Piece, fixed.id).is_for_sale is False

    notified = db.execute(
        select(Notification).where(Notification.type == "auction_cancelled")
    ).scalars().all()
    assert {n.user_id for n in notified} == {first.id, second.id}


def test_deactivation_leaves_a_piece_with_a_sale_in_flight_alone(db):
    from src.modules.pieces import listing_service

    seller = make_user(db, seller=True)
    reserved = make_piece(db, seller, status="reserved")

    result = listing_service.delist_all_for_seller(db, seller.id, reason="seller_deactivated")

    assert result["skipped"] == 1
    db.expire_all()
    assert db.get(Piece, reserved.id).status == "reserved"


def test_cancelling_an_auction_twice_is_a_noop(db):
    """Cancellation races the 1-minute close sweep, so it has to be idempotent by state."""
    from src.modules.bids import auction_cancellation

    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece = _auction(db, seller)
    make_bid(db, _auction_row(db, piece), bidder, 250_00)

    first = auction_cancellation.cancel_auction(
        db, piece, reason=auction_cancellation.REASON_SELLER_CANCELLED
    )
    second = auction_cancellation.cancel_auction(
        db, piece, reason=auction_cancellation.REASON_SELLER_CANCELLED
    )

    assert len(first) == 1
    assert second == []
