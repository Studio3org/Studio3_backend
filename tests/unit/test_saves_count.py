"""The save count on your own piece (BACKEND_CHANGES B17): the owner sees it, nobody else gets
the field, and it counts the same people B12 tells.

Runs without a database (`pytest tests/unit/test_saves_count.py --noconftest`); the same through
the API against Postgres is in tests/api/test_saves_count.py.
"""
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from src.modules.notifications import watchers
from src.modules.pieces import pieces_controller

OWNER = uuid.uuid4()


class _Capture:
    def __init__(self, result=0):
        self.statement = None
        self.result = result

    def execute(self, statement):
        self.statement = statement
        return SimpleNamespace(
            scalar_one=lambda: self.result,
            scalars=lambda: SimpleNamespace(all=lambda: []),
        )


def _sql(statement):
    return str(statement.compile(dialect=postgresql.dialect()))


def test_the_count_is_the_people_savers_of_would_tell():
    piece = SimpleNamespace(id=uuid.uuid4(), user_id=OWNER)
    counted, listed = _Capture(result=12), _Capture()
    assert watchers.count_savers(counted, piece) == 12
    watchers.savers_of(listed, piece)
    count_sql, list_sql = _sql(counted.statement), _sql(listed.statement)
    assert count_sql.startswith("SELECT count(users.id)")
    # One row per person, with the same rules: quick saves and collections, not the artist,
    # no deleted accounts, no blocks.
    assert count_sql.split("FROM", 1)[1] == list_sql.split("FROM", 1)[1]


@pytest.fixture
def detail(monkeypatch):
    piece = SimpleNamespace(id=uuid.uuid4(), user_id=OWNER, listing_type="fixed")
    author = SimpleNamespace(id=OWNER, username="sarah", name="Sarah", image=None)
    monkeypatch.setattr(pieces_controller, "piece_to_dict", lambda p, media=None: {"id": str(p.id)})
    monkeypatch.setattr(pieces_controller, "list_piece_media", lambda _db, _id: [])
    monkeypatch.setattr(pieces_controller, "get_user_by_id", lambda _db, _id: author)
    social = pieces_controller.social_dao
    for name in ("user_follows", "user_liked", "user_saved"):
        monkeypatch.setattr(social, name, lambda *_a, **_k: False)
    for name in ("count_likes", "count_comments"):
        monkeypatch.setattr(social, name, lambda *_a, **_k: 0)
    monkeypatch.setattr(pieces_controller.series_dao, "get_series_for_piece", lambda _db, _id: None)
    from src.modules.posts import posts_dao

    monkeypatch.setattr(posts_dao, "list_related_posts", lambda _db, _id: [])
    monkeypatch.setattr(watchers, "count_savers", lambda _db, _piece: 12)
    return lambda viewer: pieces_controller.enrich_piece_dict(None, piece, viewer)


def test_the_owner_sees_how_many_saved_it(detail):
    assert detail(OWNER)["savesCount"] == 12


def test_nobody_else_gets_the_field(detail):
    assert "savesCount" not in detail(uuid.uuid4())
    assert "savesCount" not in detail(None)
