"""Putting work on an event's bill.

The rule under test throughout: tagging a piece to an event for sale **ends whatever listing
it already had**. That releases real money when the piece was in an auction, so the tests
care about who is allowed to trigger it and what happens to the bidders.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.modules.events import events_dao, lineup_service
from src.shared.models.auction import (
    AUCTION_CANCELLED,
    AUCTION_LIVE,
    HOLD_RELEASED,
    Auction,
    Hold,
)
from src.shared.models.bid import BID_CANCELLED, Bid
from src.shared.models.event import PIECE_BID, PIECE_FEATURED, PIECE_SALE
from src.shared.models.piece import Piece
from src.shared.utils.app_error import AppError
from tests.factories import (
    make_auction,
    make_bid,
    make_event,
    make_event_participant,
    make_piece,
    make_user,
)


def _live_auction_piece(db, seller):
    piece = make_piece(db, seller, price_cents=300_00, status="live", listing_type="auction")
    auction = make_auction(db, piece, seller, starting_bid_cents=300_00)
    return piece, auction


# --- the tagging rule -----------------------------------------------------------------------

def test_selling_a_piece_at_an_event_ends_its_running_auction_and_refunds_bidders(db):
    seller = make_user(db, seller=True)
    a, b = make_user(db), make_user(db)
    piece, auction = _live_auction_piece(db, seller)
    bid_a = make_bid(db, auction, a, 350_00)
    bid_b = make_bid(db, auction, b, 400_00)
    event = make_event(db, seller)

    lineup_service.add_piece(
        db, event, piece, seller, mode=PIECE_BID, price_cents=500_00
    )

    db.expire_all()
    assert db.get(Auction, auction.id).status == AUCTION_CANCELLED
    for bid in (bid_a, bid_b):
        assert db.get(Bid, bid.id).status == BID_CANCELLED
        hold = db.query(Hold).filter_by(bid_id=bid.id).one()
        assert hold.status == HOLD_RELEASED, "a bidder's money was kept on a cancelled auction"


def test_selling_a_piece_at_an_event_ends_its_fixed_listing(db):
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=200_00, status="live", listing_type="fixed")
    event = make_event(db, seller)

    lineup_service.add_piece(
        db, event, piece, seller, mode=PIECE_SALE, price_cents=250_00
    )

    db.expire_all()
    piece = db.get(Piece, piece.id)
    assert piece.status == "delisted"
    assert piece.is_for_sale is False


def test_featuring_a_piece_leaves_its_listing_completely_alone(db):
    """A featured piece is a link on a page. Ending a live auction because a host wanted to
    show the work would be a catastrophic misreading of 'add to lineup'."""
    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece, auction = _live_auction_piece(db, seller)
    bid = make_bid(db, auction, bidder, 350_00)
    event = make_event(db, seller)

    lineup_service.add_piece(db, event, piece, seller, mode=PIECE_FEATURED)

    db.expire_all()
    assert db.get(Auction, auction.id).status == AUCTION_LIVE
    assert db.get(Bid, bid.id).status == "active"
    assert db.query(Hold).filter_by(bid_id=bid.id).one().status != HOLD_RELEASED


def test_a_host_cannot_sell_another_artists_work(db):
    """The featured-pieces picker offers every billed artist's work, so this is reachable
    from the UI. Ending someone else's auction and refunding their bidders is not a host's
    decision to make."""
    host = make_user(db, seller=True)
    artist = make_user(db, seller=True)
    piece, auction = _live_auction_piece(db, artist)
    event = make_event(db, host)
    make_event_participant(db, event, artist, role="artist")

    with pytest.raises(AppError) as excinfo:
        lineup_service.add_piece(
            db, event, piece, host, mode=PIECE_BID, price_cents=500_00
        )

    assert excinfo.value.status_code == 403
    db.expire_all()
    assert db.get(Auction, auction.id).status == AUCTION_LIVE, "host ended another artist's auction"


def test_a_host_may_feature_another_artists_work(db):
    host = make_user(db, seller=True)
    artist = make_user(db, seller=True)
    piece = make_piece(db, artist, price_cents=300_00, status="live")
    event = make_event(db, host)

    entry = lineup_service.add_piece(db, event, piece, host, mode=PIECE_FEATURED)

    assert entry.mode == PIECE_FEATURED
    assert entry.price_cents is None


def test_a_piece_with_a_sale_in_flight_cannot_be_relisted_at_an_event(db):
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=300_00, status="reserved")
    event = make_event(db, seller)

    with pytest.raises(AppError) as excinfo:
        lineup_service.add_piece(db, event, piece, seller, mode=PIECE_SALE, price_cents=300_00)

    assert excinfo.value.status_code == 409


def test_the_same_piece_cannot_be_added_twice(db):
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=300_00, status="live")
    event = make_event(db, seller)
    lineup_service.add_piece(db, event, piece, seller, mode=PIECE_FEATURED)

    with pytest.raises(AppError) as excinfo:
        lineup_service.add_piece(db, event, piece, seller, mode=PIECE_FEATURED)

    assert excinfo.value.status_code == 409


def test_a_selling_entry_needs_a_price(db):
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=300_00, status="live")
    event = make_event(db, seller)

    with pytest.raises(AppError) as excinfo:
        lineup_service.add_piece(db, event, piece, seller, mode=PIECE_SALE)

    assert excinfo.value.status_code == 400


def test_the_preview_reports_what_tagging_would_end(db):
    """The create flow warns before it acts, and the warning has to come from the same place
    the action does or the two drift apart."""
    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece, auction = _live_auction_piece(db, seller)
    make_bid(db, auction, bidder, 350_00)

    preview = lineup_service.preview_tagging(db, piece)

    assert preview["endsAuction"] is True
    assert preview["activeBidCount"] == 1
    assert preview["endsFixedListing"] is False


# --- publishing -------------------------------------------------------------------------------

def test_publishing_lists_the_bill_and_an_event_auction_runs_on_the_events_clock(db):
    """The window the client specified: bidding opens when the doors do and closes thirty
    minutes before the end, with no soft close — the room empties."""
    from src.modules.bids.auction_dao import EVENT_CLOSE_BUFFER

    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=300_00, status="draft")
    event = make_event(db, seller)
    lineup_service.add_piece(db, event, piece, seller, mode=PIECE_BID, price_cents=400_00)

    result = lineup_service.publish_lineup(db, event)

    assert result["auctionListings"] == 1
    db.expire_all()
    auction = db.query(Auction).filter_by(piece_id=piece.id).one()
    assert auction.event_id == event.id
    assert auction.opens_at == event.starts_at
    assert auction.closes_at == event.ends_at - EVENT_CLOSE_BUFFER
    assert auction.soft_close_enabled is False, "an event auction must not extend on a late bid"
    assert auction.duration_days is None
    assert db.get(Piece, piece.id).status == "live"


def test_publishing_lists_a_fixed_price_piece_at_its_event_price(db):
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=300_00, status="draft")
    event = make_event(db, seller)
    lineup_service.add_piece(db, event, piece, seller, mode=PIECE_SALE, price_cents=275_00)

    result = lineup_service.publish_lineup(db, event)

    assert result["fixedListings"] == 1
    db.expire_all()
    piece = db.get(Piece, piece.id)
    assert piece.price_cents == 275_00
    assert piece.listing_type == "fixed"
    assert piece.is_for_sale is True
    assert piece.status == "live"


def test_a_featured_piece_is_not_listed_by_publishing(db):
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=300_00, status="draft")
    event = make_event(db, seller)
    lineup_service.add_piece(db, event, piece, seller, mode=PIECE_FEATURED)

    result = lineup_service.publish_lineup(db, event)

    assert result == {"fixedListings": 0, "auctionListings": 0}
    db.expire_all()
    assert db.query(Auction).filter_by(piece_id=piece.id).count() == 0


def test_publishing_twice_does_not_create_a_second_auction(db):
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=300_00, status="draft")
    event = make_event(db, seller)
    lineup_service.add_piece(db, event, piece, seller, mode=PIECE_BID, price_cents=400_00)

    lineup_service.publish_lineup(db, event)
    lineup_service.publish_lineup(db, event)

    db.expire_all()
    assert db.query(Auction).filter_by(piece_id=piece.id).count() == 1


def test_an_event_too_short_to_hold_an_auction_is_refused(db):
    """Bidding closes 30 minutes before the end. On a 20-minute event that is before it
    starts, which would create an auction that closes before it opens."""
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=300_00, status="draft")
    event = make_event(db, seller, duration=timedelta(minutes=20))
    lineup_service.add_piece(db, event, piece, seller, mode=PIECE_BID, price_cents=400_00)

    with pytest.raises(AppError) as excinfo:
        lineup_service.publish_lineup(db, event)

    assert excinfo.value.status_code == 400


# --- cancelling --------------------------------------------------------------------------------

def test_cancelling_an_event_ends_its_auctions_and_refunds_bidders(db):
    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece = make_piece(db, seller, price_cents=300_00, status="draft")
    event = make_event(db, seller)
    lineup_service.add_piece(db, event, piece, seller, mode=PIECE_BID, price_cents=400_00)
    lineup_service.publish_lineup(db, event)
    db.expire_all()
    auction = db.query(Auction).filter_by(piece_id=piece.id).one()
    # Bidding is open from the event's start; place a bid directly to model one.
    bid = make_bid(db, auction, bidder, 450_00)

    result = lineup_service.cancel_lineup(db, event, "event_cancelled")

    assert result["cancelledAuctions"] == 1
    db.expire_all()
    assert db.get(Auction, auction.id).status == AUCTION_CANCELLED
    assert db.query(Hold).filter_by(bid_id=bid.id).one().status == HOLD_RELEASED
    assert db.get(Piece, piece.id).status == "delisted"


def test_cancelling_leaves_a_piece_that_has_already_sold_alone(db):
    """Someone bought it. There is an order pointing at that piece and it is not the
    event's to withdraw any more."""
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=300_00, status="draft")
    event = make_event(db, seller)
    lineup_service.add_piece(db, event, piece, seller, mode=PIECE_SALE, price_cents=300_00)
    lineup_service.publish_lineup(db, event)
    db.expire_all()
    db.get(Piece, piece.id).status = "sold"
    db.commit()

    result = lineup_service.cancel_lineup(db, event, "event_cancelled")

    assert result["delisted"] == 0
    db.expire_all()
    assert db.get(Piece, piece.id).status == "sold"


def test_removing_a_piece_does_not_resurrect_the_listing_it_replaced(db):
    """Those bidders were refunded and told the auction was over. Quietly reopening it would
    be worse than making the artist relist."""
    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece, auction = _live_auction_piece(db, seller)
    make_bid(db, auction, bidder, 350_00)
    event = make_event(db, seller)
    entry = lineup_service.add_piece(
        db, event, piece, seller, mode=PIECE_BID, price_cents=500_00
    )

    lineup_service.remove_piece(db, event, entry)

    db.expire_all()
    assert db.get(Auction, auction.id).status == AUCTION_CANCELLED
    assert db.get(Piece, piece.id).status == "delisted"


# --- listings ------------------------------------------------------------------------------------

def test_a_draft_never_appears_in_a_public_listing(db):
    host = make_user(db, seller=True)
    make_event(db, host, status="draft", title="Unfinished")
    make_event(db, host, status="published", title="Out")

    titles = [e.title for e in events_dao.list_upcoming(db)]

    assert "Out" in titles
    assert "Unfinished" not in titles


def test_an_event_already_under_way_still_counts_as_upcoming(db):
    """Dropping it the moment it starts loses exactly the events someone browsing right now
    is most likely to want."""
    host = make_user(db, seller=True)
    now = datetime.now(timezone.utc)
    make_event(
        db, host, status="published", title="Happening now",
        starts_at=now - timedelta(minutes=30), ends_at=now + timedelta(hours=2),
    )

    titles = [e.title for e in events_dao.list_upcoming(db)]

    assert "Happening now" in titles


def test_a_finished_event_drops_out_of_upcoming(db):
    host = make_user(db, seller=True)
    now = datetime.now(timezone.utc)
    make_event(
        db, host, status="published", title="Over",
        starts_at=now - timedelta(days=2), ends_at=now - timedelta(days=2, hours=-3),
    )

    assert "Over" not in [e.title for e in events_dao.list_upcoming(db)]
