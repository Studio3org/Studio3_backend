"""The artist's own auction over HTTP (BACKEND_CHANGES B38): the bids with anonymous bidder
numbers, for the artist only; and on the artist's piece page, the run that ended without a
sale with its top bid, which nobody else is sent."""
from datetime import datetime, timedelta, timezone

from src.modules.bids import auction_closer
from tests.factories import make_auction, make_bid, make_piece, make_user


def test_the_artist_reads_the_bids_and_nobody_else(db, client, auth_headers):
    seller = make_user(db, seller=True)
    first, second = make_user(db), make_user(db)
    piece = make_piece(db, seller, price_cents=300_00, listing_type="auction")
    auction = make_auction(db, piece, seller, starting_bid_cents=300_00)
    make_bid(db, auction, first, 300_00)
    make_bid(db, auction, second, 400_00)
    db.commit()

    res = client.get(f"/api/pieces/{piece.id}/auction/bids", headers=auth_headers(seller))
    assert res.status_code == 200, res.get_json()
    data = res.get_json()["data"]
    assert [(i["bidder"], i["amountCents"]) for i in data["items"]] == [(2, 400_00), (1, 300_00)]
    assert data["bidderCount"] == 2
    assert data["nextBidCents"] == 300_00
    assert "bidderId" not in data["items"][0] and "username" not in data["items"][0]

    other = client.get(f"/api/pieces/{piece.id}/auction/bids", headers=auth_headers(first))
    assert other.status_code == 404
    assert client.get(f"/api/pieces/{piece.id}/auction/bids").status_code == 401


def test_an_unsold_run_is_on_the_artists_page_only(db, client, auth_headers):
    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece = make_piece(db, seller, price_cents=300_00, listing_type="auction")
    now = datetime.now(timezone.utc)
    auction = make_auction(
        db, piece, seller, starting_bid_cents=300_00, reserve_cents=900_00,
        opens_at=now - timedelta(days=7), closes_at=now - timedelta(minutes=1),
    )
    make_bid(db, auction, bidder, 400_00)
    db.commit()

    auction_closer.close_expired_auctions()
    db.expire_all()

    mine = client.get(f"/api/pieces/{piece.id}", headers=auth_headers(seller)).get_json()["data"]
    last = mine["lastAuction"]
    assert last["status"] == "needs_seller_action"
    assert last["topBidCents"] == 400_00
    assert last["bidderCount"] == 1
    assert last["reserveCents"] == 900_00

    theirs = client.get(f"/api/pieces/{piece.id}", headers=auth_headers(bidder)).get_json()["data"]
    assert "lastAuction" not in theirs
