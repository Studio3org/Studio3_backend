"""Delete a series (BACKEND_CHANGES B15): the owner deletes it, the pieces stay, anyone else
gets 404.

Runs without a database (`pytest tests/unit/test_series_delete.py --noconftest`); the same
flow through the API against Postgres is in tests/api/test_series_delete.py.
"""
import uuid
from types import SimpleNamespace

import pytest
from flask import Flask, g

from src.modules.series import series_controller, series_dao
from src.shared.models.series import SeriesPiece
from src.shared.utils.app_error import AppError

OWNER = uuid.uuid4()


class _Query:
    def __init__(self, db, model):
        self.db, self.model, self.filters = db, model, None

    def filter_by(self, **kw):
        self.filters = kw
        return self

    def delete(self, **_kw):
        self.db.deleted_links.append((self.model, self.filters))
        return 2


class _Db:
    def __init__(self, series=None):
        self.series = series
        self.deleted_links = []
        self.deleted = []
        self.commits = 0
        self.closed = False

    def query(self, model):
        return _Query(self, model)

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _series():
    return SimpleNamespace(id=uuid.uuid4(), user_id=OWNER, name="Bodies in Color")


def test_delete_series_removes_its_links_then_itself():
    series = _series()
    db = _Db()
    series_dao.delete_series(db, series)
    assert db.deleted_links == [(SeriesPiece, {"series_id": series.id})]
    assert db.deleted == [series]
    assert db.commits == 1


@pytest.fixture
def as_user(monkeypatch):
    app = Flask(__name__)

    def run(user_id, db, series_id):
        monkeypatch.setattr(series_controller, "SessionLocal", lambda: db)
        monkeypatch.setattr(series_dao, "get_series", lambda _db, key: db.series if db.series and db.series.id == key else None)
        with app.app_context():
            g.user = {"id": str(user_id)}
            return series_controller.delete(str(series_id))

    return run


def test_the_owner_deletes_it(as_user):
    series = _series()
    db = _Db(series)
    data, status = as_user(OWNER, db, series.id)
    assert status == 200
    assert data == {"id": str(series.id), "deleted": True}
    assert db.deleted == [series]
    assert db.closed


def test_someone_elses_or_a_missing_series_is_not_found(as_user):
    series = _series()
    for user_id, series_id in ((uuid.uuid4(), series.id), (OWNER, uuid.uuid4())):
        db = _Db(series)
        with pytest.raises(AppError) as error:
            as_user(user_id, db, series_id)
        assert error.value.status_code == 404
        assert db.deleted == [] and db.deleted_links == []
        assert db.closed
