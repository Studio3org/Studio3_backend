"""The viewer's own bid on a piece (BACKEND_CHANGES B18): the status rules, without a database.

Runs with `pytest tests/unit/test_my_bid.py --noconftest`; the same flows through the API and
the close sweep against Postgres are in tests/api/test_my_bid.py.
"""
import uuid
from types import SimpleNamespace

import pytest

from src.modules.bids import my_bid

ME, THEM = uuid.uuid4(), uuid.uuid4()


def _bid(status="active", amount=400_00, bidder=ME):
    return SimpleNamespace(status=status, amount_cents=amount, bidder_id=bidder)


def _auction(status):
    return SimpleNamespace(id=uuid.uuid4(), status=status)


@pytest.mark.parametrize("status", ["live", "closing"])
def test_while_bidding_is_open_leading_or_outbid(status):
    auction = _auction(status)
    mine = [_bid(amount=400_00)]
    assert my_bid.status_for(auction, mine, _bid(amount=400_00), ME) == "leading"
    assert my_bid.status_for(auction, mine, _bid(amount=500_00, bidder=THEM), ME) == "outbid"


def test_no_bid_is_none_whatever_the_auction():
    for status in ("live", "awaiting_winner", "needs_seller_action"):
        assert my_bid.status_for(_auction(status), [], None, ME) == "none"


@pytest.mark.parametrize("status", ["awaiting_payment", "awaiting_winner", "closed_sold"])
def test_after_the_close_the_winner_has_won(status):
    mine = [_bid("outbid", 300_00), _bid("won", 400_00)]
    assert my_bid.status_for(_auction(status), mine, None, ME) == "won"


def test_a_runner_up_still_held_is_outbid_until_it_settles():
    assert my_bid.status_for(_auction("awaiting_payment"), [_bid("active")], None, ME) == "outbid"


@pytest.mark.parametrize(
    "auction_status, bid_status",
    [
        ("awaiting_winner", "lost"),  # someone else won
        ("needs_seller_action", "cancelled"),  # ended below the reserve
        ("cancelled", "cancelled"),  # the artist cancelled it
        ("awaiting_payment", "forfeited"),  # won and did not pay; it passed down
    ],
)
def test_otherwise_after_the_close_it_is_lost(auction_status, bid_status):
    assert my_bid.status_for(_auction(auction_status), [_bid(bid_status)], None, ME) == "lost"


class _Db:
    def __init__(self, bids):
        self.bids = bids

    def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: iter(self.bids))


def test_the_top_bid_is_the_most_they_bid_in_this_run(monkeypatch):
    auction = _auction("live")
    monkeypatch.setattr(my_bid, "_this_run", lambda _db, _id: auction)
    monkeypatch.setattr(my_bid.bid_dao, "get_highest_bid", lambda _db, _id: _bid(amount=450_00))
    db = _Db([_bid("outbid", 300_00), _bid("active", 450_00)])
    assert my_bid.viewer_bid(db, uuid.uuid4(), ME) == {"myTopBidCents": 450_00, "myBidStatus": "leading"}


def test_no_run_or_no_bids_is_none_and_signed_out_gets_nothing(monkeypatch):
    monkeypatch.setattr(my_bid, "_this_run", lambda _db, _id: None)
    assert my_bid.viewer_bid(_Db([]), uuid.uuid4(), ME) == {"myTopBidCents": None, "myBidStatus": "none"}
    assert my_bid.viewer_bid(_Db([]), uuid.uuid4(), None) == {}
