"""Separate buyer and seller types, and event reminders, against Postgres (BACKEND_CHANGES B14).

The seller of an order hears `sale` and the buyer `purchase`, so muting one does not mute the
other; everyone going to an event hears `event_reminder` once, the day before.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.modules.events import reminders
from src.modules.notifications import notifications_dao
from src.shared.models.notification import Notification
from tests.factories import make_event, make_order, make_piece, make_user


@pytest.fixture
def pushed(monkeypatch):
    calls = []

    def _fake_send(user_id, title, body, data=None):
        calls.append({"userId": user_id, "title": title, "body": body})
        return True

    monkeypatch.setattr(notifications_dao, "send_push", _fake_send)
    return calls


def _types(db, user):
    db.expire_all()
    return sorted(
        n.type for n in db.query(Notification).filter(Notification.user_id == user.id).all()
    )


def test_buyer_and_seller_hear_different_types(db, client, auth_headers, pushed):
    seller = make_user(db, seller=True)
    # A seller who turned off their own receipts still hears about the sale.
    seller.notification_preferences = {"push": {"purchase": False}}
    db.commit()
    buyer = make_user(db)
    piece = make_piece(db, seller, title="Tide")
    order = make_order(db, buyer=buyer, seller=seller, piece=piece)

    response = client.post(f"/api/orders/{order.id}/confirm", headers=auth_headers(buyer))

    assert response.status_code == 200, response.get_json()
    assert "sale" in _types(db, seller) and "purchase" not in _types(db, seller)
    assert "purchase" in _types(db, buyer) and "sale" not in _types(db, buyer)
    assert {p["userId"] for p in pushed} == {seller.id, buyer.id}


def _going(client, auth_headers, event, user, going=True):
    response = client.post(
        f"/api/events/{event.id}/rsvp", json={"going": going}, headers=auth_headers(user)
    )
    assert response.status_code == 200, response.get_json()


def test_everyone_going_is_reminded_once_the_day_before(db, client, auth_headers, pushed):
    host = make_user(db, seller=True)
    going, pulled_out, muted = make_user(db), make_user(db), make_user(db)
    muted.notification_preferences = {"push": {"event_reminder": False}}
    db.commit()
    event = make_event(db, host, status="published", title="Night Market",
                       starts_in=timedelta(hours=20))
    for user in (going, pulled_out, muted):
        _going(client, auth_headers, event, user)
    _going(client, auth_headers, event, pulled_out, going=False)

    now = datetime.now(timezone.utc)
    assert reminders.send_event_reminders(db, now) == 2
    # A second run, or a redelivered job, finds nobody left.
    assert reminders.send_event_reminders(db, now + timedelta(hours=1)) == 0

    assert _types(db, going) == ["event_reminder"]
    assert _types(db, muted) == ["event_reminder"]
    assert _types(db, pulled_out) == []
    assert _types(db, host) == []
    (row,) = db.query(Notification).filter(Notification.user_id == going.id).all()
    assert row.target_id == event.id and row.actor_id == host.id
    assert row.payload["message"].startswith("Night Market is ")
    # The one who turned Events off got the row, not the push.
    assert [p["userId"] for p in pushed] == [going.id]


def test_too_soon_too_far_and_unpublished_events_wait(db, client, auth_headers, pushed):
    host = make_user(db, seller=True)
    collector = make_user(db)
    too_soon = make_event(db, host, status="published", starts_in=timedelta(minutes=5))
    later = make_event(db, host, status="published", starts_in=timedelta(days=3))
    for event in (too_soon, later):
        _going(client, auth_headers, event, collector)
    make_event(db, host, status="draft", starts_in=timedelta(hours=10))

    assert reminders.send_event_reminders(db) == 0
    assert _types(db, collector) == []


def test_two_hours_before_comes_a_second_reminder_once(db, client, auth_headers, pushed):
    """Ticketing spec, "Notifications": the day before *and* 2 hours before."""
    host = make_user(db, seller=True)
    collector = make_user(db)
    event = make_event(db, host, status="published", starts_in=timedelta(minutes=90))
    _going(client, auth_headers, event, collector)

    assert reminders.send_event_reminders(db) == 1
    assert reminders.send_event_reminders(db) == 0
    assert _types(db, collector) == ["event_reminder"]
