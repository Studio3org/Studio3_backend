"""Phase 2A: the auction bugs from BACKEND_CHANGES and the founding-artist rate expiry (B3)."""
from datetime import datetime, timedelta, timezone

from src.modules.bids import bid_dao, hold_refresh, holds_service
from src.shared.config import commission
from src.shared.models.auction import AUCTION_CANCELLED, AUCTION_DRAFT, HOLD_EXPIRED, Auction
from src.shared.models.bid import BID_ACTIVE, Bid
from src.shared.models.notification import Notification
from tests.factories import make_auction, make_bid, make_piece, make_user


def _auction(db, seller, **kw):
    piece = make_piece(db, seller, price_cents=200_00, listing_type="auction")
    return piece, make_auction(db, piece, seller, starting_bid_cents=200_00, **kw)


def test_an_expired_hold_notification_opens_the_piece(db, monkeypatch):
    """It went out with target_type 'piece' and no id, so tapping it opened nothing."""
    seller, bidder = make_user(db, seller=True), make_user(db)
    piece, auction = _auction(db, seller)
    bid = make_bid(db, auction, bidder, 200_00)
    hold = bid_dao.hold_for_bid(db, bid.id)

    def _declined(*args, **kwargs):
        raise holds_service.HoldError("card expired", 402)

    monkeypatch.setattr(holds_service, "reauthorize", _declined)
    hold_refresh._refresh_one(db, hold.id)

    note = db.query(Notification).filter_by(user_id=bidder.id, type="bid_hold_expired").one()
    assert note.target_type == "piece"
    assert note.target_id == piece.id


def test_bidding_again_after_a_hold_expired_leaves_one_active_bid(db):
    seller, bidder = make_user(db, seller=True), make_user(db)
    _, auction = _auction(db, seller)
    old = make_bid(db, auction, bidder, 200_00, hold_status=HOLD_EXPIRED)

    new = bid_dao.place_bid(db, auction.id, bidder, 250_00, "pm_new_card")

    db.expire_all()
    active = db.query(Bid).filter_by(auction_id=auction.id, bidder_id=bidder.id,
                                     status=BID_ACTIVE).all()
    assert [b.id for b in active] == [new.id]
    assert db.get(Bid, old.id).status != BID_ACTIVE


def test_the_artist_can_cancel_an_auction_that_has_not_opened(db, client, auth_headers):
    """It answered 200 and left the draft auction in place."""
    seller = make_user(db, seller=True)
    piece, auction = _auction(db, seller, status=AUCTION_DRAFT)

    response = client.post(f"/api/pieces/{piece.id}/auction/cancel", headers=auth_headers(seller))

    assert response.status_code == 200, response.get_json()
    db.expire_all()
    assert db.get(Auction, auction.id).status == AUCTION_CANCELLED


def test_a_founding_artist_rate_ends_on_its_date(db):
    seller = make_user(db, seller=True, commission_bps_override=1500)
    standard = commission.default_bps(commission.SALE_ART)

    seller.commission_override_until = datetime.now(timezone.utc) + timedelta(days=30)
    assert commission.resolve_bps(seller) == 1500

    seller.commission_override_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert commission.resolve_bps(seller) == standard

    seller.commission_override_until = None
    assert commission.resolve_bps(seller) == 1500, "no end date never lapses"


def test_the_quote_says_when_the_reduced_rate_ends(db, client, auth_headers):
    until = datetime(2027, 11, 1, tzinfo=timezone.utc)
    seller = make_user(db, seller=True, commission_bps_override=1500)
    seller.commission_override_until = until
    db.commit()

    data = client.get(
        "/api/user/me/commission-quote?priceCents=100000", headers=auth_headers(seller)
    ).get_json()["data"]

    assert data["reducedRate"] is True
    assert data["reducedRateUntil"].startswith("2027-11-01")
