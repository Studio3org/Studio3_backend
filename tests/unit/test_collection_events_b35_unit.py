"""Events in collections (BACKEND_CHANGES B35), without a database: an event can be added to a
folder once it is out (a draft only by its host), its cover is the folder's cover, and a bad
type or id is refused.

Runs with `pytest tests/unit/test_collection_events_b35.py --noconftest`; the same over HTTP
is in tests/api/test_collection_events_b35.py.
"""
import uuid
from types import SimpleNamespace

import pytest
from flask import Flask, g

from src.modules.collections import collections_controller as cc
from src.shared.utils.app_error import AppError


def _event(status="published", host=None):
    return SimpleNamespace(
        id=uuid.uuid4(), status=status, host_id=host or uuid.uuid4(),
        cover_media_url="https://cdn.test/event.jpg",
    )


@pytest.mark.parametrize("status", ["published", "cancelled", "archived"])
def test_an_event_that_is_out_can_go_in_a_folder(status):
    assert cc._visible_event(_event(status), uuid.uuid4())


def test_a_draft_only_for_its_host():
    host = uuid.uuid4()
    draft = _event("draft", host)
    assert cc._visible_event(draft, host)
    assert not cc._visible_event(draft, uuid.uuid4())
    assert not cc._visible_event(None, host)


def test_an_events_picture_is_its_cover():
    assert cc._target_image("event", _event()) == "https://cdn.test/event.jpg"
    assert cc._target_image("piece", SimpleNamespace(media_url="p.jpg")) == "p.jpg"


@pytest.fixture()
def adding(monkeypatch):
    added = []
    me = uuid.uuid4()
    collection = SimpleNamespace(id=uuid.uuid4(), user_id=me)
    fake = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(cc, "SessionLocal", lambda: fake)
    monkeypatch.setattr(cc, "_require_owned_collection", lambda db, cid, uid: collection)
    monkeypatch.setattr(cc.collections_dao, "add_item", lambda db, cid, kind, tid: added.append((kind, tid)))
    monkeypatch.setattr(cc.collections_dao, "count_items", lambda db, cid: len(added))
    return SimpleNamespace(me=me, collection=collection, added=added)


def test_adding_an_event(monkeypatch, adding):
    event = _event()
    monkeypatch.setattr("src.modules.events.events_dao.get_event", lambda db, eid: event)
    app = Flask(__name__)
    with app.test_request_context(json={"targetType": "event", "targetId": str(event.id)}):
        g.user = {"id": str(adding.me)}
        data, status = cc.add_item(str(adding.collection.id))

    assert status == 200 and data == {"added": True, "itemCount": 1}
    assert adding.added == [("event", event.id)]


def test_someone_elses_draft_event_is_not_found(monkeypatch, adding):
    draft = _event("draft")
    monkeypatch.setattr("src.modules.events.events_dao.get_event", lambda db, eid: draft)
    app = Flask(__name__)
    with app.test_request_context(json={"targetType": "event", "targetId": str(draft.id)}):
        g.user = {"id": str(adding.me)}
        with pytest.raises(AppError) as error:
            cc.add_item(str(adding.collection.id))

    assert error.value.status_code == 404
    assert adding.added == []


@pytest.mark.parametrize(
    "body, code",
    [({"targetType": "user", "targetId": str(uuid.uuid4())}, 400), ({"targetType": "event", "targetId": "x"}, 404)],
)
def test_a_bad_type_or_id_is_refused(monkeypatch, adding, body, code):
    app = Flask(__name__)
    with app.test_request_context(json=body):
        g.user = {"id": str(adding.me)}
        with pytest.raises(AppError) as error:
            cc.add_item(str(adding.collection.id))
    assert error.value.status_code == code
