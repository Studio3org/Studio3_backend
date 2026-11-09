"""Auction fields on piece lists (BACKEND_CHANGES B21), without a database.

Runs with `pytest tests/unit/test_auction_list_fields.py --noconftest`; the lists themselves
through the API against Postgres are in tests/api/test_auction_list_fields.py.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.modules.bids import auction_list_fields
from src.shared.config.auction_config import bid_increment_cents

NOW = datetime.now(timezone.utc)


def _piece(listing_type="auction"):
    return SimpleNamespace(id=uuid.uuid4(), listing_type=listing_type)


def _auction(piece, status="live", reserve=None, winning_bid_id=None, starting=300_00):
    return SimpleNamespace(
        id=uuid.uuid4(), piece_id=piece.id, status=status, starting_bid_cents=starting,
        reserve_cents=reserve, opens_at=NOW - timedelta(days=1), closes_at=NOW + timedelta(hours=6),
        winning_bid_id=winning_bid_id,
    )


class _Db:
    """Answers the three queries in order: auctions, bid totals, winning bids."""

    def __init__(self, auctions, totals=(), winning=()):
        self.answers = [
            SimpleNamespace(scalars=lambda: iter(auctions)),
            SimpleNamespace(all=lambda: list(totals)),
            SimpleNamespace(all=lambda: list(winning)),
        ]
        self.queries = 0

    def execute(self, _statement):
        self.queries += 1
        return self.answers.pop(0)


def test_fixed_price_pieces_get_nothing_and_cost_no_query():
    db = _Db([])
    assert auction_list_fields.fields_for(db, [_piece("fixed"), _piece(None)]) == {}
    assert db.queries == 0


def test_a_live_auction_carries_its_bid_count_and_end():
    piece, plain = _piece(), _piece()
    auction, quiet = _auction(piece, reserve=500_00), _auction(plain)
    db = _Db([auction, quiet], totals=[(auction.id, 450_00, 12)])

    fields = auction_list_fields.fields_for(db, [piece, plain])

    assert fields[piece.id] == {
        "auctionId": str(auction.id),
        "auctionStatus": "live",
        "startingBidCents": 300_00,
        "highestBidCents": 450_00,
        "bidCount": 12,
        "minNextBidCents": 450_00 + bid_increment_cents(450_00),
        "opensAt": auction.opens_at.isoformat(),
        "auctionEndsAt": auction.closes_at.isoformat(),
        "hasReserve": True,
        "reserveMet": False,
        "winningBidCents": None,
    }
    # No bids yet: the starting bid is the next minimum.
    assert (fields[plain.id]["highestBidCents"], fields[plain.id]["bidCount"]) == (None, 0)
    assert fields[plain.id]["minNextBidCents"] == 300_00
    assert db.queries == 2


def test_never_the_reserve_amount_or_anything_about_the_viewer():
    piece = _piece()
    fields = auction_list_fields.fields_for(_Db([_auction(piece, reserve=900_00)]), [piece])[piece.id]
    assert 900_00 not in fields.values()
    assert not {"isHighestBidder", "isWinner", "awaitingPayment", "reserveCents"} & fields.keys()


def test_a_running_run_beats_one_still_settling():
    piece = _piece()
    old, new = _auction(piece, status="awaiting_payment"), _auction(piece, status="draft")
    fields = auction_list_fields.fields_for(_Db([old, new]), [piece])
    assert fields[piece.id]["auctionId"] == str(new.id)


def test_a_settling_auction_says_what_won():
    piece, winning_id = _piece(), uuid.uuid4()
    auction = _auction(piece, status="awaiting_winner", winning_bid_id=winning_id)
    fields = auction_list_fields.fields_for(_Db([auction], winning=[(winning_id, 700_00)]), [piece])
    assert fields[piece.id]["winningBidCents"] == 700_00
    assert fields[piece.id]["auctionStatus"] == "awaiting_winner"


def test_add_to_merges_into_each_dict():
    auction_piece, fixed = _piece(), _piece("fixed")
    a, f = {"id": "a"}, {"id": "f"}
    auction = _auction(auction_piece)
    auction_list_fields.add_to(_Db([auction]), [(auction_piece, a), (fixed, f), (None, None)])
    assert a["auctionId"] == str(auction.id) and a["bidCount"] == 0
    assert f == {"id": "f"}
