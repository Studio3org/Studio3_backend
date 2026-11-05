"""Auction fields in feeds, profile grids, series and Saved (BACKEND_CHANGES B21).

`SavedFolder` "Auction ends in 6h", `SavedAvailable` "Bid $1,300 · 6h left": an auction piece
carries its bid, bid count and end time in every list; a fixed-price piece carries none of it.
"""
import uuid

from src.shared.models.social import Save
from tests.factories import make_auction, make_bid, make_piece, make_user

AUCTION_KEYS = {"auctionId", "highestBidCents", "bidCount", "auctionEndsAt", "startingBidCents"}


def _world(db):
    artist, bidder, viewer = make_user(db, seller=True), make_user(db), make_user(db)
    auctioned = make_piece(db, artist, listing_type="auction", price_cents=300_00, title="Tide")
    auction = make_auction(db, auctioned, artist, starting_bid_cents=300_00)
    make_bid(db, auction, bidder, 450_00)
    fixed = make_piece(db, artist, title="Still")
    for piece in (auctioned, fixed):
        db.add(Save(id=uuid.uuid4(), user_id=viewer.id, target_type="piece", target_id=piece.id))
    db.commit()
    return artist, viewer, auctioned, fixed, auction


def _by_id(items, piece):
    return next(i for i in items if i.get("id") == str(piece.id))


def _check(items, auctioned, fixed, auction):
    a = _by_id(items, auctioned)
    assert a["listingType"] == "auction"
    assert a["auctionId"] == str(auction.id)
    assert (a["highestBidCents"], a["bidCount"]) == (450_00, 1)
    assert a["auctionEndsAt"] == auction.closes_at.isoformat()
    assert "isHighestBidder" not in a
    assert not AUCTION_KEYS & _by_id(items, fixed).keys()


def test_profile_grid_and_saved(db, client, auth_headers):
    artist, viewer, auctioned, fixed, auction = _world(db)
    headers = auth_headers(viewer)

    grid = client.get(f"/api/users/{artist.username}/pieces", headers=headers)
    assert grid.status_code == 200, grid.get_json()
    _check(grid.get_json()["data"], auctioned, fixed, auction)

    saved = client.get("/api/user/me/saved/pieces", headers=headers)
    assert saved.status_code == 200, saved.get_json()
    _check(saved.get_json()["data"], auctioned, fixed, auction)


def test_explore_feed(db, client, auth_headers):
    _, viewer, auctioned, fixed, auction = _world(db)

    feed = client.get("/api/feed/explore?limit=50", headers=auth_headers(viewer))

    assert feed.status_code == 200, feed.get_json()
    _check(feed.get_json()["data"]["items"], auctioned, fixed, auction)


def test_a_collection(db, client, auth_headers):
    _, viewer, auctioned, fixed, auction = _world(db)
    headers = auth_headers(viewer)
    made = client.post("/api/collections", json={"name": "Wishlist"}, headers=headers)
    assert made.status_code == 201, made.get_json()
    shelf = made.get_json()["data"]["id"]
    for piece in (auctioned, fixed):
        added = client.post(
            f"/api/collections/{shelf}/items",
            json={"targetType": "piece", "targetId": str(piece.id)},
            headers=headers,
        )
        assert added.status_code in (200, 201), added.get_json()

    got = client.get(f"/api/collections/{shelf}", headers=headers)

    assert got.status_code == 200, got.get_json()
    _check(got.get_json()["data"]["items"], auctioned, fixed, auction)


def test_creating_an_auction_returns_its_summary(db, client, auth_headers):
    artist = make_user(db, seller=True)
    headers = auth_headers(artist)
    upload = client.post(
        "/api/media/presign", json={"purpose": "piece", "contentType": "image/jpeg"}, headers=headers
    ).get_json()["data"]["url"]

    created = client.post(
        "/api/pieces",
        json={
            "mediaUrl": upload, "title": "Tide", "isForSale": True, "listingType": "auction",
            "priceCents": 300_00, "auctionDurationDays": 7, "medium": "Oil",
            "dimensions": {"width": 40, "height": 60, "unit": "cm"},
        },
        headers=headers,
    )

    assert created.status_code == 201, created.get_json()
    data = created.get_json()["data"]
    assert data["auctionStatus"] == "live"
    assert (data["startingBidCents"], data["bidCount"], data["minNextBidCents"]) == (300_00, 0, 300_00)
    assert data["auctionEndsAt"] is not None
