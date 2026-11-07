"""The home feed's photos, series, For You order and Available filter (BACKEND_CHANGES B29),
without a database.

Runs with `pytest tests/unit/test_feed_extras.py --noconftest`; the feeds against Postgres are in
tests/api/test_feed_b29.py.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from src.modules.feeds import feed_extras
from src.shared.models.piece import Piece

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def _sql(expr):
    compiled = expr.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False})
    return str(compiled), compiled.params


def test_the_cursor_carries_the_rank():
    item = uuid.uuid4()
    cursor = feed_extras.encode_cursor(1, NOW, item)

    assert feed_extras.decode_cursor(cursor) == (1, NOW, item)


def test_a_cursor_from_before_the_ranking_still_goes_on():
    import base64

    item = uuid.uuid4()
    old = base64.urlsafe_b64encode(f"{NOW.isoformat()}|{item}".encode()).decode()

    assert feed_extras.decode_cursor(old) == (feed_extras.RANK_FOLLOWED, NOW, item)


@pytest.mark.parametrize("cursor", ["", "nonsense", "cjF8eHx5"])
def test_a_bad_cursor_is_none(cursor):
    assert feed_extras.decode_cursor(cursor) is None


def test_rank_then_newest_then_id():
    a, b, c, d = (uuid.UUID(int=i) for i in (1, 2, 3, 4))
    rows = [
        (2, NOW, a, "rest new"),
        (0, NOW - timedelta(days=9), b, "followed old"),
        (1, NOW - timedelta(days=1), c, "taste"),
        (0, NOW, d, "followed new"),
    ]

    assert [r[3] for r in feed_extras.order_ranked(rows)] == [
        "followed new", "followed old", "taste", "rest new",
    ]


def test_nothing_to_go_on_is_one_rank_so_newest_first():
    signals = feed_extras.Signals()
    sql, params = _sql(feed_extras.piece_rank(signals))

    assert signals.empty
    assert "WHEN false" in sql
    assert params == {"param_1": 0, "param_2": 1, "param_3": 2}


def test_follows_taste_and_activity_make_the_ranks():
    followed, engaged = uuid.uuid4(), uuid.uuid4()
    signals = feed_extras.Signals(
        followed=[followed], engaged=[engaged, followed], mediums=[" Painting "], tags=["abstract"]
    )
    sql, params = _sql(feed_extras.piece_rank(signals))

    assert signals.engaged == {engaged}, "someone followed ranks as followed"
    assert "pieces.user_id IN" in sql
    assert "lower(pieces.medium) IN" in sql
    assert "pieces.mediums &&" in sql and "pieces.style_tags &&" in sql
    assert ["painting"] in params.values()
    assert ["Abstract", "abstract"] in params.values()
    post_sql, _ = _sql(feed_extras.post_rank(signals))
    assert "posts.user_id IN" in post_sql and "medium" not in post_sql


def test_after_the_cursor_is_a_later_rank_or_older_in_the_same_rank():
    item = uuid.uuid4()
    rank = feed_extras.piece_rank(feed_extras.Signals())
    sql, params = _sql(feed_extras.after_cursor(rank, Piece.created_at, Piece.id, (1, NOW, item)))

    assert "> %(param_4)s OR" in sql
    assert "(pieces.created_at, pieces.id) < (%(param_6)s, %(param_7)s::UUID)" in sql
    assert params["param_4"] == 1 and params["param_6"] == NOW and params["param_7"] == item


def test_available_is_for_sale_and_live():
    sql, params = _sql(feed_extras.available_only(select(Piece)))

    assert "pieces.is_for_sale IS true" in sql and "pieces.status = %(status_1)s" in sql
    assert params["status_1"] == "live"


@pytest.mark.parametrize("raw, wants", [("1", True), ("true", True), (None, False), ("0", False), ("", False)])
def test_available_is_asked_with_1(raw, wants):
    assert feed_extras.wants_available(raw) is wants


class _Db:
    """Answers the series queries in turn."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.statements = []

    def execute(self, stmt):
        self.statements.append(stmt)
        answer = self.answers.pop(0)
        return SimpleNamespace(all=lambda: answer, scalars=lambda: answer)


def _piece(title, status="live", for_sale=True):
    return SimpleNamespace(
        id=uuid.uuid4(), title=title, media_url=f"https://cdn.test/{title}.jpg", status=status,
        is_for_sale=for_sale, listing_type="fixed",
    )


def test_a_series_lists_its_pieces_in_order_with_state_and_photos():
    series_id = uuid.uuid4()
    one, two, three = _piece("I"), _piece("II", status="sold"), _piece("III")
    db = _Db(
        [(two.id, series_id)],  # the page's piece is in a series
        [SimpleNamespace(id=series_id, name="Paper Planes")],
        [(series_id, one), (series_id, two), (series_id, three)],
        [(one.id, 3), (two.id, 1)],  # photo counts
    )

    result = feed_extras.series_for_pieces(db, [two.id, uuid.uuid4()])

    assert list(result) == [two.id], "a piece in no series has no series"
    series = result[two.id]
    assert series["name"] == "Paper Planes"
    assert series["pieceIds"] == [str(one.id), str(two.id), str(three.id)]
    assert [(p["title"], p["listingState"], p["imageCount"]) for p in series["previewPieces"]] == [
        ("I", "available", 3), ("II", "collected", 1), ("III", "available", 1),
    ]
    members = str(db.statements[2].compile(dialect=postgresql.dialect()))
    assert "pieces.deleted_at IS NULL" in members and "ORDER BY series_pieces.series_id, series_pieces.position" in members


def test_no_series_asks_once():
    db = _Db([])

    assert feed_extras.series_for_pieces(db, [uuid.uuid4()]) == {}
    assert len(db.statements) == 1
    assert feed_extras.series_for_pieces(db, []) == {}


def test_a_series_preview_stops_at_six():
    series_id = uuid.uuid4()
    pieces = [_piece(f"P{i}") for i in range(9)]
    db = _Db(
        [(pieces[0].id, series_id)],
        [SimpleNamespace(id=series_id, name="Long")],
        [(series_id, p) for p in pieces],
        [],
    )

    series = feed_extras.series_for_pieces(db, [pieces[0].id])[pieces[0].id]

    assert len(series["pieceIds"]) == 9
    assert len(series["previewPieces"]) == feed_extras.SERIES_PREVIEW


def test_post_rank_is_by_author_only():
    sql, _ = _sql(feed_extras.post_rank(feed_extras.Signals(mediums=["Painting"])))

    assert sql.count("WHEN false") == 2, "Scenes have no medium: taste alone doesn't rank them"
