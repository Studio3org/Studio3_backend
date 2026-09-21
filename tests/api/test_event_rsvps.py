"""RSVPs — a headcount, not a ticket.

Entry is free and open, so nothing here admits anyone or charges anything. What an RSVP buys
is that the host knows how many to expect, and that there is somebody to tell when the event
is called off.
"""
from src.shared.config.database import SessionLocal
from src.shared.models.event import Event, EventRsvp
from src.shared.models.notification import Notification
from tests.factories import make_event, make_piece, make_user


def _published(db, host, **kwargs):
    return make_event(db, host, status="published", **kwargs)


# --- saying you're coming --------------------------------------------------------------------

def test_rsvping_is_free_and_immediate(db, client, auth_headers):
    host, collector = make_user(db, seller=True), make_user(db)
    event = _published(db, host)

    response = client.post(
        f"/api/events/{event.id}/rsvp", json={"going": True}, headers=auth_headers(collector)
    )

    assert response.status_code == 200, response.get_json()
    body = response.get_json()["data"]
    assert body["going"] is True
    assert body["rsvpCount"] == 1
    # Uncapped by default — entry is open, so there is no number to show.
    assert body["spotsLeft"] is None
    assert body["isFull"] is False


def test_the_viewer_sees_their_own_rsvp_on_the_event(db, client, auth_headers):
    host, collector, other = make_user(db, seller=True), make_user(db), make_user(db)
    event = _published(db, host)
    client.post(f"/api/events/{event.id}/rsvp", json={"going": True},
                headers=auth_headers(collector))

    mine = client.get(f"/api/events/{event.id}", headers=auth_headers(collector))
    theirs = client.get(f"/api/events/{event.id}", headers=auth_headers(other))

    assert mine.get_json()["data"]["viewerIsGoing"] is True
    assert theirs.get_json()["data"]["viewerIsGoing"] is False
    assert theirs.get_json()["data"]["rsvpCount"] == 1


def test_rsvping_twice_does_not_double_the_headcount(db, client, auth_headers):
    host, collector = make_user(db, seller=True), make_user(db)
    event = _published(db, host)
    payload = {"going": True}

    client.post(f"/api/events/{event.id}/rsvp", json=payload, headers=auth_headers(collector))
    second = client.post(
        f"/api/events/{event.id}/rsvp", json=payload, headers=auth_headers(collector)
    )

    assert second.get_json()["data"]["rsvpCount"] == 1


def test_cancelling_an_rsvp_frees_the_place(db, client, auth_headers):
    host, collector = make_user(db, seller=True), make_user(db)
    event = _published(db, host, capacity=1)
    client.post(f"/api/events/{event.id}/rsvp", json={"going": True},
                headers=auth_headers(collector))

    response = client.post(
        f"/api/events/{event.id}/rsvp", json={"going": False}, headers=auth_headers(collector)
    )

    assert response.get_json()["data"]["rsvpCount"] == 0
    assert response.get_json()["data"]["isFull"] is False


def test_cancelling_keeps_the_row_so_pulling_out_is_distinguishable(db, client, auth_headers):
    """"Said yes then pulled out" is a different fact from "never replied", and a host
    reading a list the morning of should be able to tell them apart."""
    host, collector = make_user(db, seller=True), make_user(db)
    event = _published(db, host)
    client.post(f"/api/events/{event.id}/rsvp", json={"going": True},
                headers=auth_headers(collector))
    client.post(f"/api/events/{event.id}/rsvp", json={"going": False},
                headers=auth_headers(collector))

    db.expire_all()
    row = db.query(EventRsvp).filter_by(event_id=event.id, user_id=collector.id).one()
    assert row.status == "cancelled"
    assert row.cancelled_at is not None


def test_cancelling_an_rsvp_you_never_made_is_not_an_error(db, client, auth_headers):
    host, collector = make_user(db, seller=True), make_user(db)
    event = _published(db, host)

    response = client.post(
        f"/api/events/{event.id}/rsvp", json={"going": False}, headers=auth_headers(collector)
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["rsvpCount"] == 0


def test_a_draft_takes_no_rsvps(db, client, auth_headers):
    host, collector = make_user(db, seller=True), make_user(db)
    event = make_event(db, host, status="draft")

    response = client.post(
        f"/api/events/{event.id}/rsvp", json={"going": True}, headers=auth_headers(collector)
    )

    assert response.status_code == 404, "a draft is not visible, let alone RSVP-able"


def test_the_host_cannot_rsvp_to_their_own_event(db, client, auth_headers):
    host = make_user(db, seller=True)
    event = _published(db, host)

    response = client.post(
        f"/api/events/{event.id}/rsvp", json={"going": True}, headers=auth_headers(host)
    )

    assert response.status_code == 400


# --- capacity ---------------------------------------------------------------------------------

def test_a_full_event_refuses_further_rsvps(db, client, auth_headers):
    host, first, second = make_user(db, seller=True), make_user(db), make_user(db)
    event = _published(db, host, capacity=1)

    ok = client.post(f"/api/events/{event.id}/rsvp", json={"going": True},
                     headers=auth_headers(first))
    full = client.post(f"/api/events/{event.id}/rsvp", json={"going": True},
                       headers=auth_headers(second))

    assert ok.status_code == 200
    assert ok.get_json()["data"]["spotsLeft"] == 0
    assert ok.get_json()["data"]["isFull"] is True
    assert full.status_code == 409


def test_the_capacity_check_waits_on_the_event_row(db, client, auth_headers):
    """Two people cannot both be given the last place.

    Proven by holding the event row from a second connection and showing that set_rsvp
    *blocks on it* rather than reading a stale count and overselling. That is the property
    that matters, and it is deterministic — an actual two-thread race against a database
    across a network is mostly a test of latency, and a thread left holding a row lock wedges
    every test that follows it.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from src.modules.events import rsvp_service
    from src.shared.models.user import User

    host, collector = make_user(db, seller=True), make_user(db)
    event = _published(db, host, capacity=1)
    db.commit()

    blocker = SessionLocal()
    worker = SessionLocal()
    try:
        # Somebody else is mid-RSVP: they hold the event row.
        blocker.execute(
            text("SELECT id FROM events WHERE id = :id FOR UPDATE"), {"id": str(event.id)}
        )
        # Fail fast rather than sit behind them, so the test cannot hang.
        worker.execute(text("SET lock_timeout = '2s'"))

        try:
            rsvp_service.set_rsvp(
                worker,
                worker.get(Event, event.id),
                worker.get(User, collector.id),
                going=True,
            )
            raise AssertionError(
                "set_rsvp counted places without taking the lock — a capped event would "
                "oversell under concurrency"
            )
        except OperationalError:
            pass  # Blocked on the lock, which is exactly right.
    finally:
        worker.rollback()
        worker.close()
        blocker.rollback()
        blocker.close()

    db.expire_all()
    assert db.query(EventRsvp).filter_by(event_id=event.id, status="going").count() == 0


def test_capacity_cannot_be_cut_below_the_people_already_coming(db, client, auth_headers):
    """They said yes in good faith, and there is no mechanism — and no product decision —
    for choosing which of them to turn away."""
    host = make_user(db, seller=True)
    event = _published(db, host)
    for _ in range(3):
        client.post(f"/api/events/{event.id}/rsvp", json={"going": True},
                    headers=auth_headers(make_user(db)))

    response = client.patch(
        f"/api/events/{event.id}", json={"capacity": 2}, headers=auth_headers(host)
    )

    assert response.status_code == 409
    db.expire_all()
    assert db.get(Event, event.id).capacity is None


def test_capacity_can_be_raised_or_cleared(db, client, auth_headers):
    host = make_user(db, seller=True)
    event = _published(db, host, capacity=2)

    raised = client.patch(f"/api/events/{event.id}", json={"capacity": 10},
                          headers=auth_headers(host))
    cleared = client.patch(f"/api/events/{event.id}", json={"capacity": None},
                           headers=auth_headers(host))

    assert raised.status_code == 200
    assert raised.get_json()["data"]["capacity"] == 10
    assert cleared.get_json()["data"]["capacity"] is None
    assert cleared.get_json()["data"]["spotsLeft"] is None


def test_capacity_must_be_a_positive_whole_number(db, client, auth_headers):
    host = make_user(db, seller=True)
    event = _published(db, host)

    assert client.patch(f"/api/events/{event.id}", json={"capacity": 0},
                        headers=auth_headers(host)).status_code == 400
    assert client.patch(f"/api/events/{event.id}", json={"capacity": "lots"},
                        headers=auth_headers(host)).status_code == 400


# --- the attendee list --------------------------------------------------------------------------

def test_the_host_can_see_who_is_coming(db, client, auth_headers):
    host, collector = make_user(db, seller=True), make_user(db)
    event = _published(db, host, capacity=5)
    client.post(f"/api/events/{event.id}/rsvp", json={"going": True},
                headers=auth_headers(collector))

    response = client.get(f"/api/events/{event.id}/attendees", headers=auth_headers(host))

    assert response.status_code == 200
    body = response.get_json()["data"]
    assert [p["username"] for p in body["attendees"]] == [collector.username]
    assert body["rsvpCount"] == 1
    assert body["spotsLeft"] == 4


def test_the_attendee_list_is_not_public(db, client, auth_headers):
    host, collector, nosy = make_user(db, seller=True), make_user(db), make_user(db)
    event = _published(db, host)
    client.post(f"/api/events/{event.id}/rsvp", json={"going": True},
                headers=auth_headers(collector))

    response = client.get(f"/api/events/{event.id}/attendees", headers=auth_headers(nosy))

    assert response.status_code == 404


def test_someone_who_pulled_out_is_not_on_the_list(db, client, auth_headers):
    host, collector = make_user(db, seller=True), make_user(db)
    event = _published(db, host)
    client.post(f"/api/events/{event.id}/rsvp", json={"going": True},
                headers=auth_headers(collector))
    client.post(f"/api/events/{event.id}/rsvp", json={"going": False},
                headers=auth_headers(collector))

    response = client.get(f"/api/events/{event.id}/attendees", headers=auth_headers(host))

    assert response.get_json()["data"]["attendees"] == []


# --- being told when it's off ---------------------------------------------------------------------

def test_cancelling_an_event_tells_the_people_who_were_coming(db, client, auth_headers):
    """The gap this closes: cancelling took down the listings and told the bidders, and said
    nothing to the people who had arranged their evening around turning up."""
    host = make_user(db, seller=True)
    a, b = make_user(db), make_user(db)
    event = _published(db, host)
    for collector in (a, b):
        client.post(f"/api/events/{event.id}/rsvp", json={"going": True},
                    headers=auth_headers(collector))

    response = client.post(
        f"/api/events/{event.id}/cancel", json={"reason": "Venue flooded."},
        headers=auth_headers(host),
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["attendeesNotified"] == 2
    db.expire_all()
    for collector in (a, b):
        note = db.query(Notification).filter_by(
            user_id=collector.id, type="event_cancelled"
        ).one()
        assert note.target_type == "event"
        assert note.target_id == event.id, "the notification has to open the event it is about"


def test_someone_who_pulled_out_is_not_told_it_was_cancelled(db, client, auth_headers):
    host, collector = make_user(db, seller=True), make_user(db)
    event = _published(db, host)
    client.post(f"/api/events/{event.id}/rsvp", json={"going": True},
                headers=auth_headers(collector))
    client.post(f"/api/events/{event.id}/rsvp", json={"going": False},
                headers=auth_headers(collector))

    response = client.post(
        f"/api/events/{event.id}/cancel", json={"reason": "Called off."},
        headers=auth_headers(host),
    )

    assert response.get_json()["data"]["attendeesNotified"] == 0


def test_cancelling_still_takes_down_the_bill(db, client, auth_headers):
    """Guard against the RSVP notice displacing what cancelling already did."""
    host, collector = make_user(db, seller=True), make_user(db)
    piece = make_piece(db, host, price_cents=300_00, status="draft")
    event = make_event(db, host)
    client.post(
        f"/api/events/{event.id}/pieces",
        json={"pieceId": str(piece.id), "mode": "bid", "priceCents": 400_00},
        headers=auth_headers(host),
    )
    client.post(f"/api/events/{event.id}/publish", headers=auth_headers(host))
    client.post(f"/api/events/{event.id}/rsvp", json={"going": True},
                headers=auth_headers(collector))

    response = client.post(
        f"/api/events/{event.id}/cancel", json={"reason": "Off."}, headers=auth_headers(host)
    )

    body = response.get_json()["data"]
    assert body["cancelledAuctions"] == 1
    assert body["attendeesNotified"] == 1
