"""The event page's host FAQs and the host's follow state (BACKEND_CHANGES B24).

FAQs round-trip through create, PATCH and the detail in the host's order; an answer without a
question is refused; the detail's follow flags say what the follow endpoint says.
"""
from datetime import datetime, timedelta, timezone

from tests.factories import make_event, make_user


def _window():
    starts = datetime.now(timezone.utc) + timedelta(days=7)
    return {"startsAt": starts.isoformat(), "endsAt": (starts + timedelta(hours=3)).isoformat()}


FAQS = [
    {"question": "Is it free?", "answer": "Yes, walk in."},
    {"question": "Parking?", "answer": "On the street after 6."},
]


def test_faqs_round_trip_in_the_hosts_order(db, client, auth_headers):
    host = make_user(db, seller=True)
    headers = auth_headers(host)

    created = client.post(
        "/api/events", json={"title": "Open studio", **_window(), "faqs": FAQS}, headers=headers
    )
    assert created.status_code == 201, created.get_json()
    event_id = created.get_json()["data"]["id"]
    assert created.get_json()["data"]["faqs"] == FAQS

    reordered = [{"question": " Parking? ", "answer": "Behind the building."}, FAQS[0]]
    patched = client.patch(f"/api/events/{event_id}", json={"faqs": reordered}, headers=headers)
    assert patched.status_code == 200, patched.get_json()

    detail = client.get(f"/api/events/{event_id}", headers=headers).get_json()["data"]
    assert detail["faqs"] == [{"question": "Parking?", "answer": "Behind the building."}, FAQS[0]]

    cleared = client.patch(f"/api/events/{event_id}", json={"faqs": []}, headers=headers)
    assert cleared.get_json()["data"]["faqs"] == []


def test_an_faq_without_a_question_is_refused(db, client, auth_headers):
    host = make_user(db, seller=True)
    event = make_event(db, host)

    response = client.patch(
        f"/api/events/{event.id}",
        json={"faqs": [{"question": "  ", "answer": "An answer to nothing"}]},
        headers=auth_headers(host),
    )

    assert response.status_code == 400
    db.refresh(event)
    assert event.faqs is None


def test_the_follow_flags_match_the_follow_endpoint(db, client, auth_headers):
    public_host = make_user(db, seller=True)
    private_host = make_user(db, seller=True, profile_visibility="private")
    viewer = make_user(db)
    headers = auth_headers(viewer)
    public_event = make_event(db, public_host, status="published")
    private_event = make_event(db, private_host, status="published")

    def flags(event):
        data = client.get(f"/api/events/{event.id}", headers=headers).get_json()["data"]
        return data["hostIsFollowing"], data["hostFollowRequested"]

    assert flags(public_event) == (False, False)
    assert flags(private_event) == (False, False)

    followed = client.post(f"/api/users/{public_host.username}/follow", headers=headers).get_json()["data"]
    asked = client.post(f"/api/users/{private_host.username}/follow", headers=headers).get_json()["data"]

    assert flags(public_event) == (followed["following"], followed["requested"]) == (True, False)
    assert flags(private_event) == (asked["following"], asked["requested"]) == (False, True)

    client.delete(f"/api/users/{public_host.username}/follow", headers=headers)
    assert flags(public_event) == (False, False)


def test_a_guest_gets_no_follow_flags_and_the_host_follows_nobody(db, client, auth_headers):
    host = make_user(db, seller=True)
    event = make_event(db, host, status="published")

    guest = client.get(f"/api/events/{event.id}").get_json()["data"]
    own = client.get(f"/api/events/{event.id}", headers=auth_headers(host)).get_json()["data"]

    assert "hostIsFollowing" not in guest
    assert guest["faqs"] == []
    assert (own["hostIsFollowing"], own["hostFollowRequested"]) == (False, False)
