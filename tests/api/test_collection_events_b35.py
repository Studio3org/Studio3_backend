"""Events in collections over HTTP (BACKEND_CHANGES B35): a published event goes in a folder,
shows in its detail with `targetType: "event"` and gives the folder its cover; someone else's
draft can't be added; it comes out again.
"""
from tests.factories import make_event, make_user


def test_an_event_goes_in_a_folder_and_comes_out(db, client, auth_headers):
    host, fan = make_user(db, seller=True), make_user(db)
    event = make_event(db, host, status="published", title="Open Studio Night")
    event.cover_media_url = "https://cdn.test/event.jpg"
    db.commit()
    headers = auth_headers(fan)

    folder = client.post("/api/collections", json={"name": "Weekends"}, headers=headers).get_json()["data"]["id"]
    added = client.post(
        f"/api/collections/{folder}/items", json={"targetType": "event", "targetId": str(event.id)}, headers=headers
    )
    assert added.status_code == 200, added.get_json()

    detail = client.get(f"/api/collections/{folder}", headers=headers).get_json()["data"]
    assert [(i["targetType"], i["id"]) for i in detail["items"]] == [("event", str(event.id))]
    assert detail["items"][0]["title"] == "Open Studio Night"
    summary = client.get("/api/collections", headers=headers).get_json()["data"][0]
    assert summary["cover"] == {
        "targetType": "event",
        "targetId": str(event.id),
        "mediaUrl": "https://cdn.test/event.jpg",
    }

    removed = client.delete(f"/api/collections/{folder}/items/event/{event.id}", headers=headers)
    assert removed.get_json()["data"]["itemCount"] == 0


def test_someone_elses_draft_event_cannot_be_added(db, client, auth_headers):
    host, fan = make_user(db, seller=True), make_user(db)
    draft = make_event(db, host)
    headers = auth_headers(fan)
    folder = client.post("/api/collections", json={"name": "Later"}, headers=headers).get_json()["data"]["id"]

    refused = client.post(
        f"/api/collections/{folder}/items", json={"targetType": "event", "targetId": str(draft.id)}, headers=headers
    )
    assert refused.status_code == 404
