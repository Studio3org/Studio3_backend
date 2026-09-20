"""Authorization holds — the money committed to a bid.

The property that matters most here is the ordering on a re-bid: the new hold is placed and
confirmed *before* the old one is released, so a card that refuses the higher amount leaves
the bidder exactly where they were rather than dropping them out of an auction they were
winning.
"""
import pytest

from src.modules.bids import auction_dao, bid_dao, holds_service
from src.shared.models.auction import (
    HOLD_CAPTURED,
    HOLD_HELD,
    HOLD_RELEASED,
    Hold,
)
from src.shared.models.bid import BID_ACTIVE, BID_OUTBID, Bid
from src.shared.utils.app_error import AppError
from tests.factories import make_auction, make_event, make_piece, make_user


def _auction(db, seller, *, starting=200_00, **kw):
    piece = make_piece(db, seller, price_cents=starting, listing_type="auction")
    return make_auction(db, piece, seller, starting_bid_cents=starting, **kw)


def _place(db, auction, bidder, amount, pm="pm_test_card"):
    return bid_dao.place_bid(db, auction.id, bidder, amount, pm)


# --- placing ----------------------------------------------------------------------------

def test_a_bid_authorises_the_bid_amount_only(db):
    """Per the product decision: the hold covers the bid. Shipping and tax are taken at
    confirmation after the win, against this same saved card."""
    seller, bidder = make_user(db, seller=True), make_user(db)
    auction = _auction(db, seller)

    bid = _place(db, auction, bidder, 200_00)

    hold = bid_dao.hold_for_bid(db, bid.id)
    assert hold.amount_cents == 200_00
    assert hold.status == HOLD_HELD
    assert hold.bidder_id == bidder.id


def test_the_first_bid_may_land_on_the_starting_bid(db):
    seller, bidder = make_user(db, seller=True), make_user(db)
    auction = _auction(db, seller, starting=200_00)

    assert bid_dao.min_next_bid_cents(db, auction) == 200_00
    assert _place(db, auction, bidder, 200_00).amount_cents == 200_00


def test_a_bid_below_the_minimum_is_refused_and_authorises_nothing(db):
    seller, bidder = make_user(db, seller=True), make_user(db)
    auction = _auction(db, seller, starting=200_00)

    with pytest.raises(AppError):
        _place(db, auction, bidder, 199_99)

    assert db.query(Hold).count() == 0, "a refused bid must not leave money held"


def test_a_seller_cannot_bid_on_their_own_auction(db):
    seller = make_user(db, seller=True)
    auction = _auction(db, seller)
    with pytest.raises(AppError, match="your own piece"):
        _place(db, auction, seller, 300_00)


# --- the ordering guarantee --------------------------------------------------------------

def test_raising_your_own_bid_moves_the_hold(db):
    seller, bidder = make_user(db, seller=True), make_user(db)
    auction = _auction(db, seller, starting=200_00)

    first = _place(db, auction, bidder, 200_00)
    second = _place(db, auction, bidder, 250_00)

    db.expire_all()
    assert db.get(Bid, first.id).status == BID_OUTBID
    assert db.get(Bid, second.id).status == BID_ACTIVE
    assert bid_dao.hold_for_bid(db, first.id).status == HOLD_RELEASED
    assert bid_dao.hold_for_bid(db, second.id).status == HOLD_HELD
    # Exactly one live hold for this bidder, at the new amount.
    live = holds_service.live_hold_for_bidder(db, auction.id, bidder.id)
    assert live.amount_cents == 250_00


def test_a_failed_new_hold_leaves_the_previous_one_standing(db, monkeypatch):
    """The whole reason for the ordering. Releasing first would drop a bidder out of an
    auction they were winning the moment their bank declined a higher amount."""
    seller, bidder = make_user(db, seller=True), make_user(db)
    auction = _auction(db, seller, starting=200_00)
    first = _place(db, auction, bidder, 200_00)

    def _decline(*args, **kwargs):
        raise holds_service.HoldError("card declined", 402)

    monkeypatch.setattr(holds_service, "place_hold", _decline)

    with pytest.raises(holds_service.HoldError):
        _place(db, auction, bidder, 250_00)

    db.rollback()
    db.expire_all()
    assert db.get(Bid, first.id).status == BID_ACTIVE, "the original bid was dropped"
    assert bid_dao.hold_for_bid(db, first.id).status == HOLD_HELD, "the original hold was released"
    assert bid_dao.get_highest_bid(db, auction.id).id == first.id


def test_being_outbid_by_someone_else_does_not_release_your_hold(db):
    """Money stays committed until close: a losing bidder is still in the running if the
    higher bid falls through, which is exactly what the winner cascade depends on."""
    seller = make_user(db, seller=True)
    first, second = make_user(db), make_user(db)
    auction = _auction(db, seller, starting=200_00)

    first_bid = _place(db, auction, first, 200_00)
    _place(db, auction, second, 225_00)

    db.expire_all()
    assert db.get(Bid, first_bid.id).status == BID_ACTIVE
    assert bid_dao.hold_for_bid(db, first_bid.id).status == HOLD_HELD
    assert bid_dao.count_active_bids(db, auction.id) == 2


# --- releasing and capturing ---------------------------------------------------------------

def test_releasing_is_idempotent(db):
    seller, bidder = make_user(db, seller=True), make_user(db)
    auction = _auction(db, seller)
    bid = _place(db, auction, bidder, 200_00)
    hold = bid_dao.hold_for_bid(db, bid.id)

    holds_service.release_hold(db, hold, "test", commit=True)
    released_at = hold.released_at
    holds_service.release_hold(db, hold, "test", commit=True)

    assert hold.status == HOLD_RELEASED
    assert hold.released_at == released_at, "a second release rewrote the record"


def test_a_released_hold_cannot_be_captured(db):
    seller, bidder = make_user(db, seller=True), make_user(db)
    auction = _auction(db, seller)
    bid = _place(db, auction, bidder, 200_00)
    hold = bid_dao.hold_for_bid(db, bid.id)
    holds_service.release_hold(db, hold, "outbid", commit=True)

    with pytest.raises(AppError, match="Cannot capture"):
        holds_service.capture_hold(db, hold, commit=True)


def test_capturing_twice_is_a_noop(db):
    seller, bidder = make_user(db, seller=True), make_user(db)
    auction = _auction(db, seller)
    bid = _place(db, auction, bidder, 200_00)
    hold = bid_dao.hold_for_bid(db, bid.id)

    holds_service.capture_hold(db, hold, commit=True)
    captured_at = hold.captured_at
    holds_service.capture_hold(db, hold, commit=True)

    assert hold.status == HOLD_CAPTURED
    assert hold.captured_at == captured_at


def test_cancelling_an_auction_releases_every_hold(db):
    seller = make_user(db, seller=True)
    first, second = make_user(db), make_user(db)
    auction = _auction(db, seller, starting=200_00)
    _place(db, auction, first, 200_00)
    _place(db, auction, second, 225_00)

    bid_dao.cancel_active_bids(db, auction.id, reason="seller_cancelled")

    db.expire_all()
    holds = db.query(Hold).all()
    assert len(holds) == 2
    assert all(h.status == HOLD_RELEASED for h in holds), "money stayed held on a cancelled auction"


# --- the reserve stays hidden ---------------------------------------------------------------

def test_the_summary_reports_reserve_met_but_never_the_amount(db):
    seller, bidder = make_user(db, seller=True), make_user(db)
    auction = _auction(db, seller, starting=200_00, reserve_cents=500_00)

    before = bid_dao.bid_summary(db, auction)
    assert before["hasReserve"] is True and before["reserveMet"] is False
    assert "reserve_cents" not in before and "reserveCents" not in before
    assert 500_00 not in [v for v in before.values() if isinstance(v, int)]

    _place(db, auction, bidder, 500_00)
    db.expire_all()
    assert bid_dao.bid_summary(db, auction)["reserveMet"] is True


def test_an_auction_with_no_reserve_reports_met(db):
    seller = make_user(db, seller=True)
    auction = _auction(db, seller)
    summary = bid_dao.bid_summary(db, auction)
    assert summary["hasReserve"] is False and summary["reserveMet"] is True


# --- timing ----------------------------------------------------------------------------------

def test_a_late_bid_extends_a_standalone_auction(db):
    from datetime import datetime, timedelta, timezone

    seller, bidder = make_user(db, seller=True), make_user(db)
    closing_soon = datetime.now(timezone.utc) + timedelta(minutes=2)
    auction = _auction(db, seller, closes_at=closing_soon)

    _place(db, auction, bidder, 200_00)

    db.expire_all()
    refreshed = db.get(type(auction), auction.id)
    assert refreshed.closes_at > closing_soon, "a late bid did not push the close out"


def test_an_event_auction_does_not_soft_close(db):
    """Bidding stops dead so the piece can change hands before the room empties."""
    from datetime import datetime, timedelta, timezone

    seller, bidder = make_user(db, seller=True), make_user(db)
    closing_soon = datetime.now(timezone.utc) + timedelta(minutes=2)
    auction = _auction(
        db, seller, closes_at=closing_soon, soft_close_enabled=False,
        event_id=make_event(db, seller, status="published").id,
    )

    _place(db, auction, bidder, 200_00)

    db.expire_all()
    assert db.get(type(auction), auction.id).closes_at == closing_soon


def test_bidding_is_refused_before_the_auction_opens(db):
    from datetime import datetime, timedelta, timezone

    seller, bidder = make_user(db, seller=True), make_user(db)
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    auction = _auction(db, seller, opens_at=later, closes_at=later + timedelta(hours=2))

    with pytest.raises(AppError, match="hasn't opened"):
        _place(db, auction, bidder, 200_00)


def test_one_running_auction_per_piece(db):
    """The partial unique index. A piece may accumulate auction history — cancel-then-relist
    is how it moves onto an event — but only one may be running."""
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=200_00, listing_type="auction")
    make_auction(db, piece, seller)

    with pytest.raises(AppError, match="already has an auction"):
        auction_dao.create_auction(
            db, piece, seller, starting_bid_cents=200_00, duration_days=7
        )
