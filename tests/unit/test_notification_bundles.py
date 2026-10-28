"""Bundled saves and follows, and the daily summary (BACKEND_CHANGES B7).

Runs without a database (`pytest tests/unit/test_notification_bundles.py --noconftest`): a small
stand-in session answers `get` by model and id, and `query(...)` from a list per model.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.modules.notifications import daily_summary, notifications_dao
from src.modules.notifications.daily_summary import send_daily_summaries, summary_text
from src.modules.notifications.notifications_dao import (
    bundle_message,
    bundle_payload,
    create_and_push,
    is_bundled,
    notification_to_dict,
    saves_and_follows_mode,
)
from src.shared.models.notification import Notification
from src.shared.models.user import User

OWNER = uuid.uuid4()
PIECE = uuid.uuid4()


def _user(name, prefs=None):
    uid = uuid.uuid4()
    return SimpleNamespace(
        id=uid, name=name, username=name.split()[0].lower(), image=None, notification_preferences=prefs,
    )


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_):
        return self

    def order_by(self, *_):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _Session:
    def __init__(self, users=(), notifications=()):
        self.users = {u.id: u for u in users}
        self.notifications = list(notifications)
        self.commits = 0

    def get(self, model, key):
        return self.users.get(key) if model is User else None

    def query(self, model):
        return _Query(self.notifications if model is Notification else [])

    def add(self, obj):
        self.notifications.insert(0, obj)

    def commit(self):
        self.commits += 1

    def refresh(self, _):
        pass


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(notifications_dao, "send_push", lambda uid, title, body, data=None: sent.append((uid, body)))
    monkeypatch.setattr(daily_summary, "send_push", lambda uid, title, body, data=None: sent.append((uid, body, data)))
    return sent


def _save(db, actor):
    return create_and_push(
        db, OWNER, "save", "New save", f"{actor.name} saved your piece",
        actor_id=actor.id, target_type="piece", target_id=PIECE,
    )


# --- the rules ----------------------------------------------------------------------------

def test_saves_and_follows_bundle_but_an_accepted_follow_request_does_not():
    assert is_bundled("save", None) and is_bundled("follow", {})
    assert not is_bundled("follow", {"accepted": True})
    assert not is_bundled("like", None) and not is_bundled("follow_request", None)


def test_the_mode_is_daily_unless_set_to_instant():
    assert saves_and_follows_mode(None) == "daily"
    assert saves_and_follows_mode({"savesAndFollowsMode": "instant"}) == "instant"
    assert saves_and_follows_mode({"savesAndFollowsMode": "weekly"}) == "daily"


def test_a_new_person_joins_at_the_front_and_a_repeat_changes_nothing():
    amara = {"username": "amara", "name": "Amara Osei", "profilePhotoUrl": None}
    payload = {"count": 1, "actors": [amara], "actorIds": ["a"]}
    kai = {"username": "kai", "name": "Kai", "profilePhotoUrl": None}
    updated, new = bundle_payload(payload, "k", kai)
    assert new and updated["count"] == 2
    assert [a["username"] for a in updated["actors"]] == ["kai", "amara"]
    assert updated["actorIds"] == ["k", "a"]
    again, new = bundle_payload(updated, "a", amara)
    assert not new and again is updated


def test_only_three_faces_are_kept():
    payload = {"count": 3, "actors": [{"username": u} for u in "abc"], "actorIds": list("abc")}
    updated, _ = bundle_payload(payload, "d", {"username": "d"})
    assert [a["username"] for a in updated["actors"]] == ["d", "a", "b"]
    assert updated["count"] == 4


def test_the_bundle_lines():
    assert bundle_message("save", 12, "Amara", "piece") == "12 people saved your piece."
    assert bundle_message("save", 2, "Amara", "post") == "2 people saved your Scene."
    assert bundle_message("follow", 5, "Cedric Okafor", "user") == "Cedric Okafor and 4 others followed you."
    assert bundle_message("follow", 2, "Cedric Okafor", "user") == "Cedric Okafor and 1 other followed you."


# --- writing ------------------------------------------------------------------------------

def test_the_first_save_starts_a_bundle_and_daily_mode_holds_the_push(pushes):
    owner, amara = _user("Owner"), _user("Amara Osei")
    owner.id = OWNER
    db = _Session([owner, amara])
    row = _save(db, amara)
    assert row.payload["count"] == 1
    assert row.payload["actors"][0]["name"] == "Amara Osei"
    assert row.payload["summaryPending"] is True
    assert pushes == []


def test_a_second_person_the_same_day_joins_the_row_and_brings_it_back_unread(pushes):
    owner, amara, kai = _user("Owner"), _user("Amara Osei"), _user("Kai Mensah")
    owner.id = OWNER
    db = _Session([owner, amara, kai])
    first = _save(db, amara)
    first.read = True
    first.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    second = _save(db, kai)
    assert second is first
    assert len(db.notifications) == 1
    assert second.payload["count"] == 2
    assert second.payload["message"] == "2 people saved your piece."
    assert second.actor_id == kai.id
    assert second.read is False
    assert second.created_at > datetime.now(timezone.utc) - timedelta(minutes=1)


def test_the_same_person_again_today_adds_nothing(pushes):
    owner, amara = _user("Owner", {"savesAndFollowsMode": "instant"}), _user("Amara Osei")
    owner.id = OWNER
    db = _Session([owner, amara])
    _save(db, amara)
    _save(db, amara)
    assert db.notifications[0].payload["count"] == 1
    assert len(pushes) == 1


def test_instant_mode_pushes_each_save_as_it_happens(pushes):
    owner = _user("Owner", {"savesAndFollowsMode": "instant"})
    owner.id = OWNER
    amara, kai = _user("Amara Osei"), _user("Kai Mensah")
    db = _Session([owner, amara, kai])
    _save(db, amara)
    _save(db, kai)
    assert [body for _, body in pushes] == ["Amara Osei saved your piece", "Kai Mensah saved your piece"]
    assert "summaryPending" not in db.notifications[0].payload


def test_a_row_from_before_bundling_counts_its_own_person(pushes):
    owner, amara, kai = _user("Owner"), _user("Amara Osei"), _user("Kai Mensah")
    owner.id = OWNER
    old = Notification(
        id=uuid.uuid4(), user_id=OWNER, type="save", actor_id=amara.id, target_type="piece",
        target_id=PIECE, payload={"message": "Amara Osei saved your piece"}, read=False,
        created_at=datetime.now(timezone.utc),
    )
    db = _Session([owner, amara, kai], [old])
    row = _save(db, kai)
    assert row is old
    assert row.payload["count"] == 2
    assert [a["name"] for a in row.payload["actors"]] == ["Kai Mensah", "Amara Osei"]


def test_an_accepted_follow_request_is_its_own_row(pushes):
    owner, cedric = _user("Owner"), _user("Cedric Okafor")
    owner.id = OWNER
    db = _Session([owner, cedric])
    create_and_push(
        db, OWNER, "follow", "Follow request accepted", "Your follow request was accepted",
        actor_id=cedric.id, target_type="user", target_id=cedric.id, payload={"accepted": True},
    )
    row = db.notifications[0]
    assert "count" not in row.payload
    assert len(pushes) == 1  # not held for the summary


def test_the_app_never_sees_the_bookkeeping():
    row = Notification(
        id=uuid.uuid4(), type="save", target_type="piece", target_id=PIECE, read=False,
        payload={"count": 2, "actors": [], "actorIds": ["a"], "summaryPending": True, "message": "x"},
        created_at=datetime.now(timezone.utc),
    )
    payload = notification_to_dict(row, None)["payload"]
    assert payload == {"count": 2, "actors": [], "message": "x"}


# --- the daily summary --------------------------------------------------------------------

def test_summary_lines():
    assert summary_text(1, "Amara Osei", 0, None) == "Amara Osei saved your work."
    assert summary_text(12, "Amara Osei", 0, None) == "12 people saved your work."
    assert summary_text(0, None, 1, "Kai") == "Kai followed you."
    assert summary_text(12, "A", 3, "K") == "12 people saved your work and 3 new people followed you."
    assert summary_text(0, None, 0, None) is None


def _pending(user_id, type, count, name, read=False):
    return Notification(
        id=uuid.uuid4(), user_id=user_id, type=type, read=read, created_at=datetime.now(timezone.utc),
        payload={"count": count, "actors": [{"name": name}], "summaryPending": True},
    )


def test_one_push_per_person_for_what_is_unread_and_a_second_run_sends_nothing(pushes):
    owner = _user("Owner")
    rows = [
        _pending(owner.id, "save", 5, "Amara Osei"),
        _pending(owner.id, "save", 7, "Kai"),
        _pending(owner.id, "follow", 3, "Cedric"),
        _pending(owner.id, "follow", 9, "Seen", read=True),
    ]
    db = _Session([owner], rows)
    assert send_daily_summaries(db) == 1
    assert pushes == [
        (owner.id, "12 people saved your work and 3 new people followed you.",
         {"type": "saves_and_follows_summary"}),
    ]
    assert all("summaryPending" not in r.payload for r in rows)
    assert send_daily_summaries(db) == 0
    assert len(pushes) == 1


def test_a_kind_turned_off_is_left_out_and_nothing_left_sends_nothing(pushes):
    owner = _user("Owner", {"push": {"save": False}})
    db = _Session([owner], [_pending(owner.id, "save", 4, "Amara")])
    assert send_daily_summaries(db) == 0
    assert pushes == []
