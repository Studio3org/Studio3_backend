"""What a push notification carries, so the app can open the right screen from a tap.

The defect this covers: `push_data` was an optional argument that only the two chat paths
ever passed. Every other push — a sale, an outbid, a declined auction payment with a
deadline on it — went out with an empty data map, so even a client that handled the tap had
nothing to route on.
"""
import uuid

import pytest

from src.modules.notifications import notifications_dao
from tests.factories import make_piece, make_user


@pytest.fixture
def sent(monkeypatch):
    """Capture what would have gone to Firebase."""
    calls = []

    def _fake_send(user_id, title, body, data=None):
        calls.append({"userId": user_id, "title": title, "body": body, "data": data})
        return True

    monkeypatch.setattr(notifications_dao, "send_push", _fake_send)
    return calls


def test_a_push_carries_the_target_the_app_needs_to_open(db, sent):
    seller = make_user(db, seller=True)
    bidder = make_user(db)
    piece = make_piece(db, seller, price_cents=500_00)

    notifications_dao.create_and_push(
        db,
        user_id=bidder.id,
        type="outbid",
        actor_id=seller.id,
        target_type="piece",
        target_id=piece.id,
        title="You've been outbid",
        body="Someone bid higher.",
    )

    assert len(sent) == 1
    data = sent[0]["data"]
    assert data["targetType"] == "piece"
    assert data["targetId"] == str(piece.id)
    assert data["type"] == "outbid"


def test_every_value_is_a_string(db, sent):
    """FCM stringifies the data map regardless. Sending it already-stringified keeps what the
    client parses identical to what is written here, rather than depending on a coercion."""
    seller = make_user(db, seller=True)
    piece = make_piece(db, seller, price_cents=100_00)

    notifications_dao.create_and_push(
        db,
        user_id=seller.id,
        type="auction_sold",
        target_type="piece",
        target_id=piece.id,
        title="Sold",
        body="Your auction sold.",
    )

    assert all(isinstance(v, str) for v in sent[0]["data"].values())


def test_a_user_target_carries_the_actors_username(db, sent):
    """A `user` target's id is a UUID, but the profile screen is addressed by username — so
    without this the tap has nothing routable and does nothing."""
    follower = make_user(db)
    followed = make_user(db)

    notifications_dao.create_and_push(
        db,
        user_id=followed.id,
        type="follow",
        actor_id=follower.id,
        target_type="user",
        target_id=follower.id,
        title="New follower",
        body=f"{follower.username} followed you.",
    )

    assert sent[0]["data"]["actorUsername"] == follower.username


def test_an_explicit_push_data_still_wins(db, sent):
    """Chat passes its own payload and must keep it — the default is a fallback, not an
    override."""
    a, b = make_user(db), make_user(db)
    custom = {"conversationId": str(uuid.uuid4())}

    notifications_dao.create_and_push(
        db,
        user_id=b.id,
        type="message",
        actor_id=a.id,
        title="New message",
        body="Hello",
        push_data=custom,
    )

    assert sent[0]["data"] == custom


def test_a_notification_with_no_target_still_sends(db, sent):
    """Not everything points somewhere. A push with no target must still go out rather than
    being dropped for want of routing data."""
    user = make_user(db)

    notifications_dao.create_and_push(
        db, user_id=user.id, type="system", title="Heads up", body="Something happened."
    )

    assert len(sent) == 1
    data = sent[0]["data"]
    assert data == {"type": "system"}


def test_a_muted_type_is_not_pushed(db, sent):
    """Regression guard: the target payload is built inside the preference check, so adding
    it must not start pushing types the recipient has turned off."""
    user = make_user(db)
    user.notification_preferences = {"push": {"follow": False}}
    db.commit()
    other = make_user(db)

    notifications_dao.create_and_push(
        db,
        user_id=user.id,
        type="follow",
        actor_id=other.id,
        target_type="user",
        target_id=other.id,
        title="New follower",
        body="Someone followed you.",
    )

    assert sent == []
