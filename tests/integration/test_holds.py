"""Bidding since Phase 7a (the 9 Nov design): a checked card, no hold — and the holds that
auctions from before then still carry, which keep settling the old way.

The property that matters for bidders: nothing is taken or reserved until they win. For the
old holds: release is idempotent, a released hold can't be captured, and capture is a no-op
the second time.
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
from tests.factories import make_auction, make_bid, make_event, make_piece, make_user


def _auction(db, seller, *, starting=200_00, **kw):
    piece = make_piece(db, seller, price_cents=starting, listing_type="auction")
    return make_auction(db, piece, seller, starting_bid_cents=starting, **kw)


def _place(db, auction, bidder, amount, pm="pm_test_card"):
    return bid_dao.place_bid(db, auction.id, bidder, amount, pm)


# --- placing ----------------------------------------------------------------------------

def test_a_bid_holds_no_money_and_keeps_the_card(db):
    """The 9 Nov design: the first bid checks a card; only the winner is ever charged."""
    seller, bidder = make_user(db, seller=True), make_user(db)
    auction = _auction(db, seller)

    bid = _place(db, auction, bidder, 200_00)

    assert bid_dao.hold_for_bid(db, bid.id) is None
    assert bid.payment_method_id == "pm_test_card"
    assert db.query(Hold).count() == 0


def test_the_first_bid_may_land_on_the_starting_bid(db):
    seller, bidder = make_user(db, seller=True), make_user(db)
    auction = _auction(db, seller, starting=200_00)

    assert bid_dao.min_next_bid_cents(db, auction) == 200_00
    assert _place(db, auction, bidder, 200_00).amount_cents == 200_00


def test_a_bid_below_the_minimum_is_refused(db):
    seller, bidder = make_user(db, seller=True), make_user(db)
    auction = _auction(db, seller, starting=200_00)

    with pytest.raises(AppError):
        _place(db, auction, bidder, 199_99)

    assert db.query(Bid).count() == 0


def test_a_seller_cannot_bid_on_their_own_auction(db):
    seller = make_user(db, seller=True)
    auction = _auction(db, seller)
    with pytest.raises(AppError, match="your own piece"):
        _place(db, auction, seller, 300_00)


def test_raising_your_own_bid_reuses_the_card(db):
    seller, bidder = make_user(db, seller=True), make_user(db)
    auction = _auction(db, seller, starting=200_00)

    first = _place(db, auction, bidder, 200_00)
    second = bid_dao.place_bid(db, auction.id, bidder, 225_00)  # no card given

    db.expire_all()
    assert db.get(Bid, first.id).status == BID_OUTBID
    assert db.get(Bid, second.id).status == BID_ACTIVE
    assert db.get(Bid, second.id).payment_method_id == "pm_test_card"


def test_a_card_that_isnt_saved_to_the_bidder_is_refused(db, stripe_enabled):
    """Charging it off-session at close would fail too late to tell them."""
    seller, bidder, other = make_user(db, seller=True), make_user(db), make_user(db)
    auction = _auction(db, seller)
    someone_elses = stripe_enabled.add_card(holds_service.ensure_customer(db, other))
    db.commit()

    with pytest.raises(AppError, match="Save this card first"):
        _place(db, auction, bidder, 200_00, pm=someone_elses["id"])


def test_being_outbid_charges_and_reserves_nothing(db):
    seller = make_user(db, seller=True)
    first, second = make_user(db), make_user(db)
    auction = _auction(db, seller, starting=200_00)

    first_bid = _place(db, auction, first, 200_00)
    _place(db, auction, second, 225_00)

    db.expire_all()
    assert db.get(Bid, first_bid.id).status == BID_ACTIVE, "still in line for a second chance"
    assert db.query(Hold).count() == 0
    assert bid_dao.count_active_bids(db, auction.id) == 2


# --- the holds of auctions from before Phase 7a ----------------------------------------------

def test_releasing_is_idempotent(db):
    seller, bidder = make_user(db, seller=True), make_user(db)
    auction = _auction(db, seller)
    hold = bid_dao.hold_for_bid(db, make_bid(db, auction, bidder, 200_00).id)

    holds_service.release_hold(db, hold, "test", commit=True)
    released_at = hold.released_at
    holds_service.release_hold(db, hold, "test", commit=True)

    assert hold.status == HOLD_RELEASED
    assert hold.released_at == released_at, "a second release rewrote the record"


def test_a_released_hold_cannot_be_captured(db):
    seller, bidder = make_user(db, seller=True), make_user(db)
    auction = _auction(db, seller)
    hold = bid_dao.hold_for_bid(db, make_bid(db, auction, bidder, 200_00).id)
    holds_service.release_hold(db, hold, "outbid", commit=True)

    with pytest.raises(AppError, match="Cannot capture"):
        holds_service.capture_hold(db, hold, commit=True)


def test_capturing_twice_is_a_noop(db):
    seller, bidder = make_user(db, seller=True), make_user(db)
    auction = _auction(db, seller)
    hold = bid_dao.hold_for_bid(db, make_bid(db, auction, bidder, 200_00).id)

    holds_service.capture_hold(db, hold, commit=True)
    captured_at = hold.captured_at
    holds_service.capture_hold(db, hold, commit=True)

    assert hold.status == HOLD_CAPTURED
    assert hold.captured_at == captured_at


def test_cancelling_an_auction_releases_every_old_hold(db):
    seller = make_user(db, seller=True)
    first, second = make_user(db), make_user(db)
    auction = _auction(db, seller, starting=200_00)
    make_bid(db, auction, first, 200_00)
    make_bid(db, auction, second, 225_00)
    db.commit()

    bid_dao.cancel_active_bids(db, auction.id, reason="seller_cancelled")

    db.expire_all()
    holds = db.query(Hold).all()
    assert len(holds) == 2
    assert all(h.status == HOLD_RELEASED for h in holds), "money stayed held on a cancelled auction"
    assert all(h.status != HOLD_HELD for h in holds)


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

def test_an_authorisation_never_asks_to_save_the_card_again(db, stripe_enabled):
    """The regression that broke every bid in production: `off_session=True` with
    `setup_future_usage` is refused by Stripe. Still guarded for the hold path that a winner's
    old-style authorisation (and its re-authorisation) uses."""
    seller, bidder = make_user(db, seller=True), make_user(db)
    piece = make_piece(db, seller, price_cents=300_00, status="live", listing_type="auction")
    auction = make_auction(db, piece, seller, starting_bid_cents=300_00)
    card = stripe_enabled.add_card(holds_service.ensure_customer(db, bidder))
    bid = make_bid(db, auction, bidder, 300_00, with_hold=False)
    db.commit()

    hold = holds_service.place_hold(db, bid, bidder, 300_00, card["id"], commit=True)

    assert hold.status == HOLD_HELD, "the authorisation must succeed"
    assert bid.status == BID_ACTIVE
