"""The Events tab's place, search and range (BACKEND_CHANGES B23), without a database.

Runs with `pytest tests/unit/test_event_tab_filters.py --noconftest`; browse, the list, the
cities and the admin Feature button against Postgres are in tests/api/test_event_tab.py.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from flask import Flask
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from src.modules.events import events_controller, events_dao
from src.shared.models.event import Event
from src.shared.utils.app_error import AppError


@pytest.mark.parametrize(
    "address, city",
    [
        ("12 Elm St, Dallas, TX", "Dallas"),
        ("Cedars Union, 1401 S Ervay St, Dallas, TX 75215", "Dallas"),
        ("1 Rue Oberkampf, 75011 Paris, France", None),
        ("Dallas", None),
        ("", None),
        (None, None),
    ],
)
def test_the_city_is_the_part_before_the_last_without_a_postcode(address, city):
    assert events_dao.city_from_address(address) == city


def _sql(stmt):
    compiled = stmt.compile(dialect=postgresql.dialect())
    return str(compiled), compiled.params


def test_the_range_is_an_overlap_so_open_runs_count():
    start = datetime(2026, 11, 5, tzinfo=timezone.utc)
    end = datetime(2026, 11, 12, tzinfo=timezone.utc)
    sql, params = _sql(events_dao.EventFilter(start=start, end=end).apply(select(Event)))
    assert "events.ends_at > " in sql and "events.starts_at < " in sql
    assert {start, end} <= set(params.values())


def test_city_is_case_insensitive_and_search_covers_title_venue_category_and_host():
    sql, params = _sql(events_dao.EventFilter(city=" Dallas ", q="night_market").apply(select(Event)))
    assert "lower(events.city) = " in sql and "dallas" in params.values()
    for column in ("events.title", "events.venue_name", "events.category"):
        assert f"{column} ILIKE " in sql
    assert "users.name ILIKE " in sql and "users.username ILIKE " in sql
    # The underscore is literal, not a wildcard.
    assert "%night\\_market%" in params.values()


def test_no_filter_adds_nothing():
    sql, _ = _sql(events_dao.EventFilter(city="  ", q="").apply(select(Event)))
    assert "WHERE" not in sql


def test_cities_group_spellings_under_the_commonest():
    rows = [("Dallas", 3), ("dallas", 1), ("Austin", 2)]
    db = SimpleNamespace(execute=lambda _s: SimpleNamespace(all=lambda: rows))
    assert events_dao.city_summaries(db) == [
        {"city": "Austin", "upcomingCount": 2},
        {"city": "Dallas", "upcomingCount": 4},
    ]


def _args(query):
    app = Flask(__name__)
    with app.test_request_context(f"/api/events/browse?{query}"):
        return events_controller._filter_args()


def test_query_args_parse_and_validate():
    where = _args("from=2026-11-05T00:00:00-06:00&to=2026-11-12&city=Dallas&q=tide")
    assert where.start == datetime(2026, 11, 5, 6, tzinfo=timezone.utc)
    assert where.end == datetime(2026, 11, 12, tzinfo=timezone.utc)
    assert (where.city, where.q) == ("Dallas", "tide")
    for bad in ("from=soon", "from=2026-11-12&to=2026-11-05", "q=" + "x" * 101):
        with pytest.raises(AppError) as error:
            _args(bad)
        assert error.value.status_code == 400
