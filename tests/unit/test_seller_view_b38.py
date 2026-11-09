"""The artist's own auction (BACKEND_CHANGES B38), without a database: bidders numbered by
their first bid and kept for the run, newest bid first, the next bidder's best offer, and the
run that ended (top bid from every bid, since a close without a sale voids them). Only the
artist may read the bids.

Runs with `pytest tests/unit/test_seller_view_b38.py --noconftest`; the same over HTTP is in
tests/api/test_seller_view_b38.py.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from flask import Flask, g

from src.modules.bids import auction_controller, seller_view
from src.shared.utils.app_error import AppError

T0 = datetime(2026, 11, 9, 9, tzinfo=timezone.utc)
A, B, C, D = (uuid.uuid4() for _ in range(4))


def _bid(bidder, amount, minutes, status="active"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        bidder_id=bidder,
        amount_cents=amount,
        created_at=T0 + timedelta(minutes=minutes),
        status=status,
    )


def _auction(status="live", winning_bid_id=None, reserve=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        winning_bid_id=winning_bid_id,
        starting_bid_cents=400_00,
        reserve_cents=reserve,
        closed_at=None,
    )


# The board: Bidder 4 $400 (2d), Bidder 1 $800, Bidder 2 $1,100, Bidder 3 $1,150,
# Bidder 1 $1,250, Bidder 3 $1,300 (2m).
BOARD = [
    _bid(D, 400_00, 0),
    _bid(A, 800_00, 10),
    _bid(B, 1100_00, 20),
    _bid(C, 1150_00, 30),
    _bid(A, 1250_00, 40),
    _bid(C, 1300_00, 50),
]


def test_bidders_are_numbered_by_their_first_bid_and_keep_it():
    out = seller_view.history(_auction(), BOARD)
    assert [(i["bidder"], i["amountCents"]) for i in out["items"]] == [
        (4, 1300_00),
        (2, 1250_00),
        (4, 1150_00),
        (3, 1100_00),
        (2, 800_00),
        (1, 400_00),
    ]
    assert out["bidCount"] == 6
    assert out["bidderCount"] == 4
    assert out["items"][0]["createdAt"] == (T0 + timedelta(minutes=50)).isoformat()


def test_nobody_is_named():
    out = seller_view.history(_auction(), BOARD)
    for item in out["items"]:
        assert set(item) == {"bidder", "amountCents", "createdAt", "isTop"}


def test_the_top_bidder_and_the_next_best_offer():
    out = seller_view.history(_auction(), BOARD)
    assert [i["isTop"] for i in out["items"]] == [True, False, True, False, False, False]
    assert out["nextBidCents"] == 1250_00


def test_once_there_is_a_winner_the_next_offer_is_everyone_elses_best():
    # The winner is A (their $1,250 won, say C's card failed earlier and was forfeited).
    won = BOARD[4]
    out = seller_view.history(_auction("awaiting_payment", winning_bid_id=won.id), BOARD)
    assert out["nextBidCents"] == 1300_00
    assert [i["isTop"] for i in out["items"]][:2] == [False, True]


def test_no_bids_yet():
    out = seller_view.history(_auction(), [])
    assert out["items"] == [] and out["bidderCount"] == 0 and out["nextBidCents"] is None


def test_the_run_that_ended_reads_every_bid():
    voided = [_bid(A, 800_00, 0, "cancelled"), _bid(B, 1300_00, 5, "cancelled")]
    out = seller_view.summary(_auction("needs_seller_action", reserve=1500_00), voided)
    assert out["status"] == "needs_seller_action"
    assert out["topBidCents"] == 1300_00
    assert out["bidCount"] == 2 and out["bidderCount"] == 2
    assert out["reserveCents"] == 1500_00 and out["startingBidCents"] == 400_00

    none = seller_view.summary(_auction("closed_no_bids"), [])
    assert none["topBidCents"] is None and none["bidderCount"] == 0


def test_last_auction_only_when_nothing_is_running_or_settling(monkeypatch):
    ended = _auction("closed_no_bids")
    state = SimpleNamespace(running=None, settling=None)
    monkeypatch.setattr(seller_view.auction_dao, "get_running_auction", lambda db, pid: state.running)
    monkeypatch.setattr(seller_view.auction_dao, "get_settling_auction", lambda db, pid: state.settling)
    monkeypatch.setattr(seller_view.my_bid, "_this_run", lambda db, pid: state.running or state.settling or ended)
    monkeypatch.setattr(seller_view, "_bids", lambda db, aid: [])
    assert seller_view.last_auction(None, uuid.uuid4())["status"] == "closed_no_bids"
    state.running = _auction("live")
    assert seller_view.last_auction(None, uuid.uuid4()) is None
    state.running, state.settling = None, _auction("awaiting_payment")
    assert seller_view.last_auction(None, uuid.uuid4()) is None


def test_a_piece_that_never_had_an_auction(monkeypatch):
    monkeypatch.setattr(seller_view.my_bid, "_this_run", lambda db, pid: None)
    out = seller_view.bid_history(None, uuid.uuid4())
    assert out["items"] == [] and out["auctionId"] is None


@pytest.fixture()
def owned(monkeypatch):
    owner = uuid.uuid4()
    piece = SimpleNamespace(id=uuid.uuid4(), user_id=owner)
    monkeypatch.setattr(auction_controller, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(auction_controller, "get_piece", lambda db, pid: piece if pid == piece.id else None)
    monkeypatch.setattr(seller_view, "bid_history", lambda db, pid: {"items": ["ok"]})
    return SimpleNamespace(owner=owner, piece=piece)


def _call(piece_id, viewer):
    app = Flask(__name__)
    with app.test_request_context():
        g.user = {"id": str(viewer)}
        return auction_controller.bid_history(str(piece_id))


def test_only_the_artist_reads_the_bids(owned):
    assert _call(owned.piece.id, owned.owner) == ({"items": ["ok"]}, 200)
    with pytest.raises(AppError) as err:
        _call(owned.piece.id, uuid.uuid4())
    assert err.value.status_code == 404
    with pytest.raises(AppError) as err:
        _call(uuid.uuid4(), owned.owner)
    assert err.value.status_code == 404
