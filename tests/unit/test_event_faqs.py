"""The event page's host FAQs and the host's follow state (BACKEND_CHANGES B24), without a
database.

Runs with `pytest tests/unit/test_event_faqs.py --noconftest`; the round trip over HTTP and the
follow flags against Postgres are in tests/api/test_event_faqs.py.
"""
import uuid
from types import SimpleNamespace

import pytest

from src.modules.events import events_controller, events_dao
from src.modules.social import social_dao
from src.shared.utils.app_error import AppError


def test_faqs_are_trimmed_and_kept_in_order():
    faqs = events_dao.validate_faqs([
        {"question": "  Is it free? ", "answer": " Yes. "},
        {"question": "Parking?", "answer": "On the street."},
        {"question": "Kids?"},
    ])

    assert faqs == [
        {"question": "Is it free?", "answer": "Yes."},
        {"question": "Parking?", "answer": "On the street."},
        {"question": "Kids?", "answer": ""},
    ]


@pytest.mark.parametrize("raw", [None, [], [{"question": " ", "answer": ""}]])
def test_no_faqs_is_stored_as_none(raw):
    assert events_dao.validate_faqs(raw) is None


def test_a_wholly_blank_row_is_dropped():
    faqs = events_dao.validate_faqs([{"question": "Parking?", "answer": "Yes"}, {"question": "", "answer": ""}])

    assert faqs == [{"question": "Parking?", "answer": "Yes"}]


@pytest.mark.parametrize(
    "raw",
    [
        [{"question": "", "answer": "An answer to nothing"}],
        [{"question": "x" * (events_dao.MAX_FAQ_QUESTION + 1), "answer": "a"}],
        [{"question": "q", "answer": "x" * (events_dao.MAX_FAQ_ANSWER + 1)}],
        [{"question": f"Q{i}", "answer": "a"} for i in range(events_dao.MAX_FAQS + 1)],
        "Is it free?",
        ["Is it free?"],
    ],
    ids=["no question", "long question", "long answer", "too many", "not a list", "not an object"],
)
def test_bad_faqs_are_refused(raw):
    with pytest.raises(AppError) as error:
        events_dao.validate_faqs(raw)
    assert error.value.status_code == 400


def test_ten_faqs_are_allowed():
    raw = [{"question": f"Q{i}", "answer": "a"} for i in range(events_dao.MAX_FAQS)]

    assert len(events_dao.validate_faqs(raw)) == events_dao.MAX_FAQS


@pytest.mark.parametrize(
    "status, following, requested",
    [(None, False, False), ("accepted", True, False), ("pending", False, True)],
)
def test_the_host_follow_flags_read_the_follow_row(monkeypatch, status, following, requested):
    viewer, host = uuid.uuid4(), uuid.uuid4()
    row = SimpleNamespace(status=status) if status else None
    asked = []

    def get_follow(db, follower_id, following_id):
        asked.append((follower_id, following_id))
        return row

    monkeypatch.setattr(social_dao, "get_follow", get_follow)
    flags = events_controller._host_follow(None, SimpleNamespace(host_id=host), viewer)

    assert flags == {"hostIsFollowing": following, "hostFollowRequested": requested}
    assert asked == [(viewer, host)]


def test_the_host_does_not_follow_themselves(monkeypatch):
    host = uuid.uuid4()
    monkeypatch.setattr(social_dao, "get_follow", lambda *a: pytest.fail("not asked"))

    flags = events_controller._host_follow(None, SimpleNamespace(host_id=host), host)

    assert flags == {"hostIsFollowing": False, "hostFollowRequested": False}
