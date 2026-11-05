"""The viewer's own bid on piece detail, before and after the close (BACKEND_CHANGES B18)."""
from datetime import datetime, timedelta, timezone

from src.modules.bids import auction_closer
from tests.factories import make_auction, make_bid, make_piece, make_user


def _mine(client, auth_headers, piece, viewer=None):
    headers = auth_headers(viewer) if viewer is not None else None
    data = client.get(f"/api/pieces/{piece.id}", headers=headers).get_json()["data"]
    return data.get("myTopBidCents", "absent"), data.get("myBidStatus", "absent")


def _ending(db, seller, **kw):
    piece = make_piece(db, seller, price_cents=300_00, listing_type="auction")
    now = datetime.now(timezone.utc)
    auction = make_auction(
        db, piece, seller, starting_bid_cents=300_00,
        opens_at=now - timedelta(days=7), closes_at=now - timedelta(minutes=1), **kw,
    )
    return piece, auction


def test_while_open_leading_outbid_none_and_signed_out(db, client, auth_headers):
    seller = make_user(db, seller=True)
    leader, beaten, watcher = make_user(db), make_user(db), make_user(db)
    piece = make_piece(db, seller, price_cents=300_00, listing_type="auction")
    auction = make_auction(db, piece, seller, starting_bid_cents=300_00)
    make_bid(db, auction, beaten, 300_00)
    make_bid(db, auction, leader, 400_00)

    assert _mine(client, auth_headers, piece, leader) == (400_00, "leading")
    # From any phone: the server knows they were outbid and what they last bid.
    assert _mine(client, auth_headers, piece, beaten) == (300_00, "outbid")
    assert _mine(client, auth_headers, piece, watcher) == (None, "none")
    assert _mine(client, auth_headers, piece, seller) == (None, "none")
    assert _mine(client, auth_headers, piece) == ("absent", "absent")


def test_after_a_sale_the_winner_won_and_the_rest_lost(db, client, auth_headers):
    seller = make_user(db, seller=True)
    winner, loser = make_user(db), make_user(db)
    piece, auction = _ending(db, seller)
    make_bid(db, auction, loser, 300_00)
    make_bid(db, auction, winner, 450_00)

    auction_closer.close_expired_auctions()
    db.expire_all()

    assert _mine(client, auth_headers, piece, winner) == (450_00, "won")
    assert _mine(client, auth_headers, piece, loser) == (300_00, "lost")


def test_below_the_reserve_everyone_lost_but_keeps_their_top_bid(db, client, auth_headers):
    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece, auction = _ending(db, seller, reserve_cents=900_00)
    make_bid(db, auction, bidder, 400_00)

    auction_closer.close_expired_auctions()
    db.expire_all()

    # `BidReserveNotMet` "Your top bid", though the auction summary is gone from the piece.
    assert _mine(client, auth_headers, piece, bidder) == (400_00, "lost")


def test_a_new_run_starts_everyone_at_none(db, client, auth_headers):
    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece, auction = _ending(db, seller, reserve_cents=900_00)
    make_bid(db, auction, bidder, 400_00)
    auction_closer.close_expired_auctions()
    relist = client.post(
        f"/api/pieces/{piece.id}/auction/relist",
        json={"durationDays": 5, "startingBidCents": 250_00},
        headers=auth_headers(seller),
    )
    assert relist.status_code == 201, relist.get_json()
    db.expire_all()

    assert _mine(client, auth_headers, piece, bidder) == (None, "none")
