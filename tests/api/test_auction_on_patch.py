"""The Auction follows the listing on PATCH (BACKEND_CHANGES B16).

Before this, PATCH validated auction terms and never touched the Auction row: List later at
auction made no Auction (every bid 400), a draft auction taken live stayed in draft (bids 409,
never closed), term edits never reached it, and auction to fixed left it running.
"""
from datetime import timedelta

from src.modules.bids import auction_dao
from src.shared.models.auction import Auction
from src.shared.models.piece import Piece
from tests.factories import make_auction, make_bid, make_event, make_piece, make_user

AUCTION_TERMS = {
    "isForSale": True,
    "listingType": "auction",
    "priceCents": 200_00,
    "auctionDurationDays": 7,
}


def _running(db, piece):
    db.expire_all()
    return auction_dao.get_running_auction(db, piece.id)


def _bid(client, auth_headers, piece, bidder, amount=200_00):
    return client.post(
        f"/api/pieces/{piece.id}/bids",
        json={"amountCents": amount, "paymentMethodId": "pm_test_card"},
        headers=auth_headers(bidder),
    )


def test_listing_later_at_auction_opens_one_that_takes_bids(db, client, auth_headers):
    seller, bidder = make_user(db, seller=True), make_user(db)
    piece = make_piece(db, seller, is_for_sale=False)

    response = client.patch(f"/api/pieces/{piece.id}", json=AUCTION_TERMS, headers=auth_headers(seller))

    assert response.status_code == 200, response.get_json()
    auction = _running(db, piece)
    assert auction is not None and auction.status == "live"
    assert auction.starting_bid_cents == 200_00 and auction.duration_days == 7
    assert auction.closes_at - auction.opens_at == timedelta(days=7)
    assert _bid(client, auth_headers, piece, bidder).status_code == 201


def test_a_draft_auction_opens_when_the_piece_goes_live(db, client, auth_headers):
    seller, bidder = make_user(db, seller=True), make_user(db)
    piece = make_piece(db, seller, status="draft", is_for_sale=False)
    headers = auth_headers(seller)
    assert client.patch(f"/api/pieces/{piece.id}", json=AUCTION_TERMS, headers=headers).status_code == 200
    assert _running(db, piece).status == "draft"

    response = client.patch(f"/api/pieces/{piece.id}", json={"status": "live"}, headers=headers)

    assert response.status_code == 200, response.get_json()
    assert _running(db, piece).status == "live"
    assert _bid(client, auth_headers, piece, bidder).status_code == 201


def test_term_edits_before_the_first_bid_reach_the_auction(db, client, auth_headers):
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, is_for_sale=False)
    headers = auth_headers(seller)
    client.patch(f"/api/pieces/{piece.id}", json={**AUCTION_TERMS, "reserveCents": 300_00}, headers=headers)
    opened = _running(db, piece)

    response = client.patch(
        f"/api/pieces/{piece.id}",
        json={"priceCents": 250_00, "auctionDurationDays": 10},
        headers=headers,
    )

    assert response.status_code == 200, response.get_json()
    auction = _running(db, piece)
    assert auction.id == opened.id
    assert (auction.starting_bid_cents, auction.duration_days) == (250_00, 10)
    # The reserve was left out of the patch, so it stays.
    assert auction.reserve_cents == 300_00
    assert auction.closes_at - auction.opens_at == timedelta(days=10)

    # Above the reserve it stands: refused, not silently dropped.
    above = client.patch(f"/api/pieces/{piece.id}", json={"priceCents": 400_00}, headers=headers)
    assert above.status_code == 400
    assert client.patch(
        f"/api/pieces/{piece.id}", json={"reserveCents": 500_00}, headers=headers
    ).status_code == 200
    assert _running(db, piece).reserve_cents == 500_00


def test_terms_are_frozen_once_it_has_bids(db, client, auth_headers):
    seller, bidder = make_user(db, seller=True), make_user(db)
    piece = make_piece(db, seller, listing_type="auction", price_cents=200_00)
    auction = make_auction(db, piece, seller, starting_bid_cents=200_00)
    make_bid(db, auction, bidder, 250_00)

    response = client.patch(
        f"/api/pieces/{piece.id}", json={"reserveCents": 900_00}, headers=auth_headers(seller)
    )

    assert response.status_code == 409
    assert _running(db, piece).reserve_cents is None


def test_auction_to_fixed_cancels_the_auction(db, client, auth_headers):
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, listing_type="auction", price_cents=200_00)
    auction = make_auction(db, piece, seller, starting_bid_cents=200_00)

    response = client.patch(
        f"/api/pieces/{piece.id}",
        json={"listingType": "fixed", "priceCents": 300_00},
        headers=auth_headers(seller),
    )

    assert response.status_code == 200, response.get_json()
    db.expire_all()
    ended = db.get(Auction, auction.id)
    assert ended.status == "cancelled" and ended.cancelled_reason == "listing_changed"
    assert _running(db, piece) is None
    assert db.get(Piece, piece.id).status == "live"


def test_off_the_profile_ends_the_run_and_back_on_starts_a_new_one(db, client, auth_headers):
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, listing_type="auction", price_cents=200_00)
    first = make_auction(db, piece, seller, starting_bid_cents=200_00, duration_days=5)
    headers = auth_headers(seller)

    assert client.patch(f"/api/pieces/{piece.id}", json={"status": "delisted"}, headers=headers).status_code == 200
    waiting = _running(db, piece)
    assert db.get(Auction, first.id).status == "cancelled"
    assert waiting.status == "draft" and waiting.duration_days == 5

    assert client.patch(f"/api/pieces/{piece.id}", json={"status": "live"}, headers=headers).status_code == 200
    again = _running(db, piece)
    assert again.id == waiting.id and again.status == "live"
    assert again.closes_at - again.opens_at == timedelta(days=5)


def test_an_event_auction_is_changed_from_the_event(db, client, auth_headers):
    seller = make_user(db, seller=True)
    event = make_event(db, seller, status="published")
    piece = make_piece(db, seller, listing_type="auction", price_cents=200_00)
    make_auction(db, piece, seller, starting_bid_cents=200_00, event_id=event.id)

    response = client.patch(
        f"/api/pieces/{piece.id}", json={"priceCents": 300_00}, headers=auth_headers(seller)
    )

    assert response.status_code == 409
    assert _running(db, piece).starting_bid_cents == 200_00
