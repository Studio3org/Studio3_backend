"""The Events tab (BACKEND_CHANGES B23): Featured, Near me, search and the range.

Featured honours the range; city and search combine with category; a run that opened before
`from` and ends after it is included; the cities list counts what is coming up; the team
features an event from the admin Events page.
"""
from datetime import datetime, timedelta, timezone

from src.modules.admin.admin_auth import SESSION_STARTED_KEY, SESSION_USER_KEY
from src.shared.models.event import Event
from tests.factories import make_event, make_user


def _published(db, host, **kw):
    kw.setdefault("address", "1401 S Ervay St, Dallas, TX 75215")
    event = make_event(db, host, status="published", **kw)
    event.city = kw.get("city") or (
        None if not kw["address"] else kw["address"].split(",")[-2].strip()
    )
    db.commit()
    return event


def _ids(cards):
    return {c["id"] for c in cards}


def _iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def test_featured_is_the_team_pick_in_the_range(db, client):
    host = make_user(db, seller=True)
    soon = _published(db, host, title="Soon", starts_in=timedelta(days=2))
    later = _published(db, host, title="Later", starts_in=timedelta(days=20))
    plain = _published(db, host, title="Plain", starts_in=timedelta(days=1))
    for event in (soon, later):
        event.featured_at = datetime.now(timezone.utc)
    db.commit()
    now = datetime.now(timezone.utc)

    week = client.get(f"/api/events/browse?from={_iso(now)}&to={_iso(now + timedelta(days=7))}")
    month = client.get(
        f"/api/events/browse?from={_iso(now + timedelta(days=10))}&to={_iso(now + timedelta(days=30))}"
    )

    assert week.status_code == 200, week.get_json()
    assert week.get_json()["data"]["featured"]["id"] == str(soon.id)
    assert month.get_json()["data"]["featured"]["id"] == str(later.id)
    # Not picked by the team, so never Featured, however soon.
    assert _ids(week.get_json()["data"]["upcoming"]) >= {str(soon.id), str(plain.id)}


def test_a_run_already_open_is_in_the_range(db, client):
    host = make_user(db, seller=True)
    now = datetime.now(timezone.utc)
    open_run = _published(
        db, host, title="Open run", starts_at=now - timedelta(days=3), ends_at=now + timedelta(days=10)
    )
    over = _published(db, host, title="Next month", starts_in=timedelta(days=40))

    data = client.get(
        f"/api/events?from={_iso(now)}&to={_iso(now + timedelta(days=7))}"
    ).get_json()["data"]

    assert str(open_run.id) in _ids(data["events"])
    assert str(over.id) not in _ids(data["events"])


def test_city_and_search_combine_with_category(db, client):
    host = make_user(db, seller=True, name="Amara Osei")
    dallas_walk = _published(db, host, title="Deep Ellum walk", category="gallery_walk")
    dallas_class = _published(db, host, title="Deep Ellum class", category="workshop")
    austin_walk = _published(
        db, host, title="Deep Ellum in Austin", category="gallery_walk",
        address="1 Congress Ave, Austin, TX",
    )

    narrowed = client.get(
        "/api/events?category=gallery_walk&city=dallas&q=deep ellum"
    ).get_json()["data"]["events"]
    by_host = client.get("/api/events?q=amara&city=Austin").get_json()["data"]["events"]

    assert _ids(narrowed) == {str(dallas_walk.id)}
    assert str(dallas_class.id) not in _ids(narrowed)
    assert _ids(by_host) == {str(austin_walk.id)}
    assert narrowed[0]["city"] == "Dallas"


def test_cities_and_saving_an_address_fills_the_city(db, client, auth_headers):
    host = make_user(db, seller=True)
    _published(db, host)
    _published(db, host)
    _published(db, host, address="1 Congress Ave, Austin, TX")
    draft = make_event(db, host, status="draft")

    patched = client.patch(
        f"/api/events/{draft.id}",
        json={"address": "200 Main St, Fort Worth, TX 76102"},
        headers=auth_headers(host),
    )
    assert patched.status_code == 200, patched.get_json()
    db.expire_all()
    assert db.get(Event, draft.id).city == "Fort Worth"

    cities = client.get("/api/events/cities").get_json()["data"]["cities"]
    # Drafts don't count.
    assert {"city": "Dallas", "upcomingCount": 2} in cities
    assert {"city": "Austin", "upcomingCount": 1} in cities
    assert all(c["city"] != "Fort Worth" for c in cities)


def test_the_team_features_an_event_from_admin(db, client):
    admin = make_user(db, is_admin=True)
    event = _published(db, make_user(db, seller=True))
    with client.session_transaction() as session:
        session[SESSION_USER_KEY] = str(admin.id)
        session[SESSION_STARTED_KEY] = datetime.now(timezone.utc).timestamp()

    on = client.post(f"/admin/events/{event.id}/feature", data={"featured": "1"})
    assert on.status_code == 302
    db.expire_all()
    assert db.get(Event, event.id).featured_at is not None
    assert client.get("/api/events/browse").get_json()["data"]["featured"]["id"] == str(event.id)

    client.post(f"/admin/events/{event.id}/feature", data={"featured": "0"})
    db.expire_all()
    assert db.get(Event, event.id).featured_at is None
