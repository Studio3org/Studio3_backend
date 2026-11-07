"""Events in the home feed, Explore and on profiles (BACKEND_CHANGES B27).

Ended, draft and cancelled events never appear; the feed has at most one event, on its first
page, and only for an app that asks; a host's upcoming events and the profile's Upcoming show
are the soonest still to come; Explore's Upcoming shows carry their pieces.
"""
import uuid
from datetime import datetime, timedelta, timezone

from src.shared.models.block import Block
from src.shared.models.event import EventPiece
from src.shared.models.social import Follow
from tests.factories import make_event, make_piece, make_user


def _gone(db, host):
    """An ended, a draft and a cancelled event: none of them may show anywhere."""
    now = datetime.now(timezone.utc)
    return {
        str(make_event(db, host, title="Ended", status="published",
                       starts_at=now - timedelta(days=2), ends_at=now - timedelta(days=1)).id),
        str(make_event(db, host, title="Draft", status="draft", starts_in=timedelta(days=1)).id),
        str(make_event(db, host, title="Cancelled", status="cancelled", starts_in=timedelta(days=1)).id),
    }


def _bill(db, event, artist, count):
    for i in range(count):
        piece = make_piece(db, artist, title=f"Work {i}")
        db.add(EventPiece(id=uuid.uuid4(), event_id=event.id, piece_id=piece.id, sort_order=i))
    db.commit()


def test_a_hosts_upcoming_events_and_the_profile_card(db, client, auth_headers):
    host = make_user(db, seller=True)
    viewer = make_user(db)
    gone = _gone(db, host)
    later = make_event(db, host, title="Later", status="published", starts_in=timedelta(days=9))
    soon = make_event(db, host, title="Soon", status="published", starts_in=timedelta(days=2))

    listed = client.get(f"/api/users/{host.username}/events?scope=upcoming", headers=auth_headers(viewer))
    profile = client.get(f"/api/user/{host.username}", headers=auth_headers(viewer))
    own = client.get("/api/user/me", headers=auth_headers(host))

    assert listed.status_code == 200, listed.get_json()
    assert [e["id"] for e in listed.get_json()["data"]["events"]] == [str(soon.id), str(later.id)]
    assert profile.get_json()["data"]["upcomingEvent"]["id"] == str(soon.id)
    assert own.get_json()["data"]["upcomingEvent"]["id"] == str(soon.id)
    assert not gone & {e["id"] for e in listed.get_json()["data"]["events"]}


def test_nothing_coming_up_is_no_card_and_blocked_or_private_is_hidden(db, client, auth_headers):
    host = make_user(db, seller=True)
    private = make_user(db, seller=True, profile_visibility="private")
    viewer = make_user(db)
    _gone(db, host)
    make_event(db, private, status="published")

    assert client.get(f"/api/user/{host.username}").get_json()["data"]["upcomingEvent"] is None
    locked = client.get(f"/api/user/{private.username}", headers=auth_headers(viewer)).get_json()["data"]
    assert "upcomingEvent" not in locked
    assert client.get(
        f"/api/users/{private.username}/events?scope=upcoming", headers=auth_headers(viewer)
    ).status_code == 403

    db.add(Block(id=uuid.uuid4(), blocker_id=host.id, blocked_id=viewer.id))
    db.commit()
    assert client.get(
        f"/api/users/{host.username}/events?scope=upcoming", headers=auth_headers(viewer)
    ).status_code == 404


def test_the_feed_has_one_event_on_its_first_page_for_an_app_that_asks(db, client, auth_headers):
    artist = make_user(db, seller=True)
    friend = make_user(db, seller=True)
    stranger = make_user(db, seller=True)
    viewer = make_user(db)
    for i in range(6):
        make_piece(db, artist, title=f"Feed {i}")
    db.add(Follow(id=uuid.uuid4(), follower_id=viewer.id, following_id=friend.id, status="accepted"))
    db.commit()
    gone = _gone(db, friend)
    make_event(db, stranger, title="Sooner", status="published", starts_in=timedelta(days=1))
    followed = make_event(db, friend, title="Friend's", status="published", starts_in=timedelta(days=6))
    make_event(db, friend, title="Too far out", status="published", starts_in=timedelta(days=20))
    headers = auth_headers(viewer)

    def events(url):
        body = client.get(url, headers=headers).get_json()["data"]
        return [i for i in body["items"] if i.get("type") == "event"], body

    old_app, _ = events("/api/feed/for-you?limit=3")
    first, body = events("/api/feed/for-you?limit=3&include=events")
    following, _ = events("/api/feed/following?include=events")
    second, _ = events(f"/api/feed/for-you?limit=3&include=events&cursor={body['nextCursor']}")

    assert old_app == []
    assert [e["event"]["id"] for e in first] == [str(followed.id)]
    assert body["items"][3]["type"] == "event"
    assert len(following) <= 1
    assert second == []
    assert not gone & {e["event"]["id"] for e in first + following}


def test_explore_carries_upcoming_shows_with_their_pieces(db, client):
    host = make_user(db, seller=True)
    artist = make_user(db, seller=True, name="Amara Osei")
    show = make_event(db, host, title="Group show", status="published", starts_in=timedelta(days=4))
    _bill(db, show, artist, 3)
    make_event(db, host, title="Empty bill", status="published", starts_in=timedelta(days=1))
    for gone_id in _gone(db, host):
        _bill(db, db.get(type(show), uuid.UUID(gone_id)), artist, 1)

    body = client.get("/api/feed/explore").get_json()["data"]
    filtered = client.get("/api/feed/explore?medium=painting").get_json()["data"]
    for_you = client.get("/api/feed/for-you").get_json()["data"]

    assert [s["title"] for s in body["upcomingShows"]] == ["Group show"]
    pieces = body["upcomingShows"][0]["pieces"]
    assert [p["title"] for p in pieces] == ["Work 0", "Work 1", "Work 2"]
    assert pieces[0]["artistName"] == "Amara Osei"
    assert "upcomingShows" not in filtered
    assert "upcomingShows" not in for_you
