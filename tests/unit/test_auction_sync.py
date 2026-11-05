"""The Auction follows the listing on PATCH (BACKEND_CHANGES B16): the rules, without a database.

Runs with `pytest tests/unit/test_auction_sync.py --noconftest`; the same flows through the API
against Postgres are in tests/api/test_auction_on_patch.py.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.modules.pieces import auction_sync, piece_state
from src.shared.utils.app_error import AppError

SELLER = SimpleNamespace(id=uuid.uuid4())
NOW = datetime.now(timezone.utc)


def _piece(**kw):
    base = dict(id=uuid.uuid4(), is_for_sale=True, listing_type="auction", status=piece_state.LIVE)
    return SimpleNamespace(**{**base, **kw})


def _auction(**kw):
    base = dict(
        id=uuid.uuid4(), status="live", event_id=None, starting_bid_cents=200_00,
        reserve_cents=None, duration_days=7, opens_at=NOW - timedelta(days=1),
        closes_at=NOW + timedelta(days=6), extended_once=False, cancelled_reason=None,
        closed_at=None,
    )
    return SimpleNamespace(**{**base, **kw})


def _listing(price=200_00, days=7, reserve=None):
    return {"is_for_sale": True, "listing_type": "auction", "price_cents": price,
            "auction_duration_days": days, "reserve_cents": reserve}


class _Db:
    def __init__(self, last=None):
        self.last = last
        self.flushes = 0

    def flush(self):
        self.flushes += 1

    def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.last)


@pytest.fixture
def world(monkeypatch):
    """The running auction, what was created and opened, and how many bids."""
    state = SimpleNamespace(running=None, created=[], opened=[], bids=0)

    def create_auction(_db, piece, seller, **kw):
        auction = _auction(status="draft", starting_bid_cents=kw["starting_bid_cents"],
                           duration_days=kw["duration_days"], reserve_cents=kw["reserve_cents"],
                           opens_at=None, closes_at=None)
        state.created.append(auction)
        return auction

    def open_auction(_db, auction, commit=True):
        auction.status = "live"
        auction.opens_at = NOW
        state.opened.append(auction)
        return auction

    dao = auction_sync.auction_dao
    monkeypatch.setattr(dao, "get_running_auction", lambda _db, _id, lock=False: state.running)
    monkeypatch.setattr(dao, "create_auction", create_auction)
    monkeypatch.setattr(dao, "open_auction", open_auction)
    monkeypatch.setattr(auction_sync.bid_dao, "count_active_bids", lambda _db, _id: state.bids)
    return state


def test_listed_at_auction_with_none_creates_and_opens_one(world):
    opened = auction_sync.sync(_Db(), _piece(), SELLER, _listing(reserve=300_00))
    (made,) = world.created
    assert (made.starting_bid_cents, made.duration_days, made.reserve_cents) == (200_00, 7, 300_00)
    assert world.opened == [made] and opened is made


def test_a_draft_piece_gets_a_draft_auction_that_waits(world):
    assert auction_sync.sync(_Db(), _piece(status=piece_state.DRAFT), SELLER, _listing()) is None
    assert len(world.created) == 1 and world.opened == []


def test_going_live_opens_the_draft(world):
    world.running = _auction(status="draft")
    assert auction_sync.sync(_Db(), _piece(), SELLER, None) is world.running
    assert world.running.status == "live"


def test_new_terms_move_to_the_auction_and_the_close_follows_the_length(world):
    world.running = _auction()
    auction_sync.sync(_Db(), _piece(), SELLER, _listing(price=250_00, days=10, reserve=400_00))
    a = world.running
    assert (a.starting_bid_cents, a.duration_days, a.reserve_cents) == (250_00, 10, 400_00)
    assert a.closes_at == a.opens_at + timedelta(days=10)
    assert world.created == [] and world.opened == []


def test_terms_are_refused_with_bids_while_closing_or_after_an_extension(world):
    for running, bids in ((_auction(), 1), (_auction(status="closing"), 0)):
        world.running, world.bids = running, bids
        with pytest.raises(AppError) as error:
            auction_sync.sync(_Db(), _piece(), SELLER, _listing(price=300_00))
        assert error.value.status_code == 409
        assert running.starting_bid_cents == 200_00
    world.running, world.bids = _auction(extended_once=True), 0
    with pytest.raises(AppError, match="extended"):
        auction_sync.sync(_Db(), _piece(), SELLER, _listing(days=10))


def test_a_length_already_passed_is_refused(world):
    world.running = _auction(opens_at=NOW - timedelta(days=5))
    with pytest.raises(AppError) as error:
        auction_sync.sync(_Db(), _piece(), SELLER, _listing(days=3))
    assert error.value.status_code == 400
    assert world.running.duration_days == 7


def test_unchanged_terms_are_left_alone_even_with_bids(world):
    world.running, world.bids = _auction(), 3
    assert auction_sync.sync(_Db(), _piece(), SELLER, _listing()) is None


def test_no_longer_an_auction_cancels_it(world):
    for piece in (_piece(listing_type="fixed"), _piece(is_for_sale=False, listing_type=None)):
        world.running = _auction()
        auction_sync.sync(_Db(), piece, SELLER, None)
        assert world.running.status == "cancelled"
        assert world.running.cancelled_reason == "listing_changed"
        assert world.running.closed_at is not None


def test_cancelling_one_with_bids_is_refused(world):
    world.running, world.bids = _auction(), 1
    with pytest.raises(AppError, match="Cancel the auction"):
        auction_sync.sync(_Db(), _piece(listing_type="fixed"), SELLER, None)
    assert world.running.status == "live"


def test_off_the_profile_ends_the_run_and_a_fresh_draft_waits(world):
    world.running = _auction(duration_days=5, reserve_cents=300_00)
    auction_sync.sync(_Db(), _piece(status=piece_state.DELISTED), SELLER, None)
    assert world.running.status == "cancelled" and world.running.cancelled_reason == "unlisted"
    (fresh,) = world.created
    assert (fresh.status, fresh.duration_days, fresh.reserve_cents) == ("draft", 5, 300_00)


def test_an_event_auction_is_left_to_the_event(world):
    world.running = _auction(event_id=uuid.uuid4())
    assert auction_sync.sync(_Db(), _piece(listing_type="fixed"), SELLER, None) is None
    assert world.running.status == "live"


def test_set_live_again_without_terms_reuses_the_last_run(world):
    last = _auction(status="closed_no_bids", duration_days=5, starting_bid_cents=150_00)
    opened = auction_sync.sync(_Db(last=last), _piece(), SELLER, None)
    assert (opened.starting_bid_cents, opened.duration_days) == (150_00, 5)


def test_live_without_terms_or_a_past_run_asks_for_the_length(world):
    with pytest.raises(AppError, match="Auction duration"):
        auction_sync.sync(_Db(), _piece(), SELLER, None)


def test_a_status_change_off_the_profile_with_no_auction_does_nothing(world):
    assert auction_sync.sync(_Db(), _piece(status=piece_state.DRAFT), SELLER, None) is None
    assert world.created == []
