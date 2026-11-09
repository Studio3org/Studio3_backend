"""Saved pieces and artists you follow (BACKEND_CHANGES B12): who hears, what it says, and the
one-row-a-day rule.

Runs without a database (`pytest tests/unit/test_watchers.py --noconftest`); the same flows
through the API against Postgres are in tests/api/test_watchers.py.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from src.modules.notifications import watchers
from src.modules.pieces import piece_state

ARTIST = uuid.uuid4()


def _piece(**kw):
    base = dict(
        id=uuid.uuid4(), user_id=ARTIST, title="Paper Planes III", status=piece_state.LIVE,
        is_for_sale=True, listing_type="fixed", price_cents=95000,
    )
    return SimpleNamespace(**{**base, **kw})


# --- what it says ---------------------------------------------------------------------------

def test_money():
    assert watchers.money(95000) == "$950"
    assert watchers.money(120000) == "$1,200"
    assert watchers.money(95050) == "$950.50"


def test_the_saved_piece_lines():
    title = "Paper Planes III"
    assert watchers.saved_piece_message(watchers.SAVED_PIECE_LISTED, title, {"priceCents": 95000}) == (
        "Paper Planes III, which you saved, is now available for $950."
    )
    assert watchers.saved_piece_message(
        watchers.SAVED_PIECE_PRICE, title, {"priceCents": 85000, "wasPriceCents": 95000}
    ) == "Paper Planes III, which you saved, is now $850 (was $950)."
    assert watchers.saved_piece_message(
        watchers.SAVED_PIECE_PRICE, title, {"priceCents": 95000, "wasPriceCents": 95000}
    ) == "Paper Planes III, which you saved, is now $950."
    assert watchers.saved_piece_message(
        watchers.SAVED_PIECE_AUCTION, title, {"priceCents": 80000, "biddingOpen": True}
    ) == "Paper Planes III, which you saved, is up for auction. Bidding starts at $800."
    assert "is going to auction" in watchers.saved_piece_message(
        watchers.SAVED_PIECE_AUCTION, title, {"priceCents": 80000, "biddingOpen": False}
    )


def test_the_artist_lines():
    assert watchers.artist_post_message("Amara Osei", 3, 0) == "Amara Osei posted 3 new pieces."
    assert watchers.artist_post_message("Amara Osei", 0, 1) == "Amara Osei posted a new Scene."
    assert watchers.artist_post_message("Amara Osei", 2, 1) == "Amara Osei posted 2 new pieces and a new Scene."


# --- when savers hear -----------------------------------------------------------------------

@pytest.fixture
def told(monkeypatch):
    calls = []
    monkeypatch.setattr(
        watchers, "_tell_savers", lambda _db, piece, type, payload: calls.append((type, payload)) or 1
    )
    return calls


def test_going_on_sale_tells_savers(told):
    before = watchers.listing_state(_piece(is_for_sale=False, listing_type=None))
    watchers.after_listing_change(None, _piece(), before)
    assert told == [(watchers.SAVED_PIECE_LISTED, {"priceCents": 95000})]


def test_a_draft_or_delisted_piece_going_live_on_sale_is_listed_too(told):
    for status in (piece_state.DRAFT, piece_state.DELISTED):
        told.clear()
        watchers.after_listing_change(None, _piece(), watchers.listing_state(_piece(status=status)))
        assert told[0][0] == watchers.SAVED_PIECE_LISTED


def test_a_new_price_while_on_sale_tells_savers_with_the_old_one(told):
    watchers.after_listing_change(None, _piece(price_cents=85000), watchers.listing_state(_piece()))
    assert told == [(watchers.SAVED_PIECE_PRICE, {"priceCents": 85000, "wasPriceCents": 95000})]


def test_nothing_else_tells_savers(told):
    same = _piece()
    watchers.after_listing_change(None, same, watchers.listing_state(same))
    # Coming off sale, or an auction's terms: not this.
    watchers.after_listing_change(None, _piece(is_for_sale=False), watchers.listing_state(_piece()))
    auction = _piece(listing_type="auction")
    watchers.after_listing_change(None, _piece(listing_type="auction", price_cents=1), watchers.listing_state(auction))
    assert told == []


def test_an_auction_opening_says_whether_bidding_has_started(told):
    now = datetime.now(timezone.utc)
    watchers.auction_opened(None, _piece(), SimpleNamespace(starting_bid_cents=80000, opens_at=now))
    later = now + timedelta(days=3)
    watchers.auction_opened(None, _piece(), SimpleNamespace(starting_bid_cents=80000, opens_at=later))
    assert told[0] == (
        watchers.SAVED_PIECE_AUCTION,
        {"priceCents": 80000, "biddingOpen": True, "opensAt": now.isoformat()},
    )
    assert told[1][1]["biddingOpen"] is False


# --- telling them, once a day ---------------------------------------------------------------

class _Db:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def get(self, _model, key):
        return SimpleNamespace(id=key, name="Amara Osei", username="amarasart") if key == ARTIST else None


@pytest.fixture
def world(monkeypatch):
    """Two watchers: `a` has no row today, `b` has one; pushes and rows are recorded."""
    a, b = uuid.uuid4(), uuid.uuid4()
    state = SimpleNamespace(a=a, b=b, pushed=[], rows={}, fail=set())

    def create_and_push(_db, **kw):
        if kw["user_id"] in state.fail:
            raise RuntimeError("push failed")
        state.pushed.append(kw)

    monkeypatch.setattr(watchers.notifications_dao, "create_and_push", create_and_push)
    monkeypatch.setattr(watchers, "savers_of", lambda _db, _piece: [a, b])
    monkeypatch.setattr(watchers, "followers_of", lambda _db, _artist: [a, b])
    monkeypatch.setattr(
        watchers, "_todays_row", lambda _db, user_id, _type, **_k: state.rows.get(user_id)
    )
    return state


def test_a_saver_with_no_row_today_gets_one_and_a_push(world):
    piece = _piece()
    world.rows[world.b] = SimpleNamespace(payload={"priceCents": 90000, "wasPriceCents": 99000}, read=True)
    db = _Db()
    told = watchers._tell_savers(db, piece, watchers.SAVED_PIECE_PRICE, {"priceCents": 85000, "wasPriceCents": 95000})
    assert told == 2
    # `a`: a new row, pushed, about the piece, from its artist.
    (push,) = world.pushed
    assert push["user_id"] == world.a
    assert push["type"] == watchers.SAVED_PIECE_PRICE
    assert (push["target_type"], push["target_id"], push["actor_id"]) == ("piece", piece.id, ARTIST)
    assert push["body"] == "Paper Planes III, which you saved, is now $850 (was $950)."
    assert push["payload"]["message"] == push["body"]
    # `b`: today's row updated, unread, measured against the price they first heard; no push.
    row = world.rows[world.b]
    assert row.read is False
    assert row.payload["wasPriceCents"] == 99000
    assert row.payload["message"] == "Paper Planes III, which you saved, is now $850 (was $990)."
    assert db.commits == 1


def test_one_failure_does_not_stop_the_rest(world):
    world.fail.add(world.a)
    db = _Db()
    assert watchers._tell_savers(db, _piece(), watchers.SAVED_PIECE_LISTED, {"priceCents": 95000}) == 1
    assert db.rollbacks == 1


def test_an_artists_first_post_of_the_day_pushes_and_the_next_joins_it(world):
    first = uuid.uuid4()
    world.rows[world.b] = SimpleNamespace(
        payload={"pieces": 2, "scenes": 0}, read=True, target_type="piece", target_id=first,
    )
    scene = uuid.uuid4()
    assert watchers.artist_posted(_Db(), ARTIST, "scene", scene) == 2
    (push,) = world.pushed
    assert push["user_id"] == world.a
    assert push["type"] == watchers.FOLLOWED_ARTIST_POST
    assert (push["target_type"], push["target_id"]) == ("post", scene)
    assert push["title"] == "Amara Osei"
    assert push["body"] == "Amara Osei posted a new Scene."
    row = world.rows[world.b]
    assert row.payload["message"] == "Amara Osei posted 2 new pieces and a new Scene."
    assert (row.target_type, row.target_id, row.read) == ("post", scene, False)


def test_an_unknown_artist_tells_nobody(world):
    assert watchers.artist_posted(_Db(), uuid.uuid4(), "piece", uuid.uuid4()) == 0
    assert world.pushed == []


# --- who --------------------------------------------------------------------------------------

def _sql(statement):
    return str(statement.compile(dialect=postgresql.dialect()))


class _Capture:
    def __init__(self):
        self.statement = None

    def execute(self, statement):
        self.statement = statement
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))


def test_savers_are_quick_saves_and_collections_without_the_artist_deleted_or_blocked():
    db = _Capture()
    watchers.savers_of(db, _piece())
    sql = _sql(db.statement)
    assert "saves.target_type" in sql and "collection_items.target_type" in sql
    assert "users.id != " in sql
    assert "users.deleted_at IS NULL" in sql
    assert "blocks.blocker_id" in sql and "blocks.blocked_id" in sql


def test_followers_are_accepted_ones_only():
    db = _Capture()
    watchers.followers_of(db, ARTIST)
    sql = _sql(db.statement)
    assert "follows.following_id" in sql and "follows.status" in sql
    assert "users.deleted_at IS NULL" in sql
