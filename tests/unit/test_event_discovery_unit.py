"""Events in the home feed, Explore and on profiles (BACKEND_CHANGES B27), without a database.

Runs with `pytest tests/unit/test_event_discovery.py --noconftest`; the routes against Postgres
are in tests/api/test_event_discovery.py.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from flask import Flask
from sqlalchemy.dialects import postgresql

from src.modules.events import event_discovery
from src.modules.feeds import feeds_controller

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
DALLAS = (32.78, -96.80)
FORT_WORTH = (32.75, -97.33)  # about 50 km
AUSTIN = (30.27, -97.74)  # about 290 km


def _event(days, host=None, at=None):
    lat, lng = at or (None, None)
    return SimpleNamespace(
        id=uuid.uuid4(), host_id=host or uuid.uuid4(), starts_at=NOW + timedelta(days=days),
        latitude=lat, longitude=lng,
    )


def test_distance_is_great_circle_km():
    assert event_discovery.distance_km(*DALLAS, *DALLAS) == pytest.approx(0)
    assert event_discovery.distance_km(*DALLAS, *FORT_WORTH) == pytest.approx(50, abs=5)
    assert event_discovery.distance_km(*DALLAS, *AUSTIN) == pytest.approx(290, abs=10)


def test_a_followed_host_comes_first_then_near_then_soonest():
    friend = uuid.uuid4()
    soonest = _event(1)
    near = _event(5, at=FORT_WORTH)
    far = _event(2, at=AUSTIN)
    followed = _event(10, host=friend)

    ranked = event_discovery.rank_feed_events(
        [soonest, near, far, followed], followed={friend}, viewer_lat=DALLAS[0], viewer_lng=DALLAS[1]
    )

    assert ranked == [followed, near, soonest, far]


def test_without_a_location_or_follows_it_is_the_soonest():
    later, sooner = _event(9, at=FORT_WORTH), _event(3)

    assert event_discovery.rank_feed_events([later, sooner], followed=set()) == [sooner, later]


@pytest.mark.parametrize("count, slot", [(0, -1), (1, 1), (3, 3), (20, 3)])
def test_the_feed_event_goes_after_the_third_item(count, slot):
    assert event_discovery.feed_slot(count) == slot


class _Capture:
    """Stands in for the session: keeps the statement, answers nothing."""

    def __init__(self):
        self.statements = []

    def execute(self, stmt):
        self.statements.append(stmt)
        return SimpleNamespace(scalars=lambda: [], all=lambda: [])

    def get(self, *a):
        return None


def _sql(stmt):
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_every_query_is_published_and_not_over():
    db = _Capture()
    event_discovery.list_host_upcoming(db, uuid.uuid4())
    event_discovery.feed_event(db, None, hidden={uuid.uuid4()})
    event_discovery.upcoming_shows(db, hidden={uuid.uuid4()})

    assert len(db.statements) == 3
    for stmt in db.statements:
        sql = _sql(stmt)
        assert "events.status = %(status_1)s" in sql
        assert "events.ends_at > %(ends_at_1)s" in sql
        assert stmt.compile(dialect=postgresql.dialect()).params["status_1"] == "published"
    host, feed, shows = (_sql(s) for s in db.statements)
    assert "ORDER BY events.starts_at ASC" in host
    assert "events.starts_at < " in feed and "NOT IN" in feed
    assert "event_pieces.event_id" in shows and "NOT IN" in shows


def test_a_show_with_no_pieces_left_is_left_out(monkeypatch):
    show = _event(2)

    class Db(_Capture):
        def execute(self, stmt):
            self.statements.append(stmt)
            if len(self.statements) == 1:
                return SimpleNamespace(scalars=lambda: [show])
            return SimpleNamespace(all=lambda: [])

    assert event_discovery.upcoming_shows(Db()) == []


def test_pieces_by_someone_hidden_are_dropped():
    show, hidden_artist, artist = _event(2), uuid.uuid4(), uuid.uuid4()

    def row(owner, title):
        entry = SimpleNamespace(mode="featured", price_cents=None)
        piece = SimpleNamespace(id=uuid.uuid4(), title=title, media_url="m")
        return entry, piece, SimpleNamespace(id=owner, username=title.lower(), name=title)

    class Db(_Capture):
        def execute(self, stmt):
            self.statements.append(stmt)
            if len(self.statements) == 1:
                return SimpleNamespace(scalars=lambda: [show])
            return SimpleNamespace(all=lambda: [row(hidden_artist, "Gone"), row(artist, "Kept")])

    [(event, pieces)] = event_discovery.upcoming_shows(Db(), hidden={hidden_artist})

    assert event is show
    assert [p["title"] for p in pieces] == ["Kept"]
    assert set(pieces[0]) == {
        "pieceId", "title", "mediaUrl", "mode", "priceCents", "artistUsername", "artistName",
    }


@pytest.mark.parametrize(
    "query, wants",
    [("", False), ("?include=events", True), ("?include=media,events", True), ("?include=event", False)],
)
def test_the_feed_event_is_only_for_an_app_that_asks(query, wants):
    with Flask(__name__).test_request_context(f"/api/feed/for-you{query}"):
        assert feeds_controller._wants_events() is wants


def _with_event(monkeypatch, query, items):
    event = _event(3)
    monkeypatch.setattr(event_discovery, "feed_event", lambda db, viewer, hidden: event)
    from src.modules.events import events_controller

    monkeypatch.setattr(events_controller, "event_card", lambda db, e, viewer: {"id": str(e.id)})
    result = {"items": list(items), "nextCursor": None}
    with Flask(__name__).test_request_context(f"/api/feed/for-you{query}"):
        feeds_controller._add_feed_event(None, result, None, set())
    return result["items"], event


def test_one_event_after_the_third_item_of_the_first_page(monkeypatch):
    items, event = _with_event(monkeypatch, "?include=events", [{"id": i} for i in range(5)])

    assert [i.get("type") for i in items] == [None, None, None, "event", None, None]
    assert items[3]["event"] == {"id": str(event.id)}


@pytest.mark.parametrize("query", ["", "?include=events&cursor=abc"])
def test_no_event_unless_asked_or_past_the_first_page(monkeypatch, query):
    items, _ = _with_event(monkeypatch, query, [{"id": i} for i in range(5)])

    assert all(i.get("type") != "event" for i in items)


def test_an_empty_feed_gets_no_event(monkeypatch):
    items, _ = _with_event(monkeypatch, "?include=events", [])

    assert items == []
