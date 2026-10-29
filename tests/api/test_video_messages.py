"""Video messages through the API (BACKEND_CHANGES B11).

The upload URL comes from /api/media/presign (the local media store here, as no bucket is
configured in tests); the message carries it as `videoUrl`.
"""
from tests.factories import make_user

MB = 1024 * 1024


def _data(response) -> dict:
    return response.get_json()["data"]


def _presign(client, headers, content_type, length=None):
    body = {"purpose": "chat", "contentType": content_type}
    if length is not None:
        body["contentLength"] = length
    return client.post("/api/media/presign", json=body, headers=headers)


def test_a_video_message_round_trips(db, client, auth_headers):
    me, other = make_user(db), make_user(db)
    headers = auth_headers(me)
    video = _data(_presign(client, headers, "video/mp4", 3 * MB))["url"]
    poster = _data(_presign(client, headers, "image/jpeg", 40_000))["url"]

    started = client.post(
        "/api/conversations",
        json={"username": other.username, "videoUrl": video, "thumbnailUrl": poster},
        headers=headers,
    )
    assert started.status_code == 201, started.get_json()
    conversation_id = _data(started)["id"]

    thread = _data(client.get(f"/api/conversations/{conversation_id}", headers=headers))
    message = thread["messages"]["items"][0]
    assert message["videoUrl"] == video
    assert message["thumbnailUrl"] == poster
    assert message["body"] is None

    inbox = _data(client.get("/api/conversations", headers=headers))["items"]
    assert inbox[0]["preview"] == "Video"

    sent = client.post(
        f"/api/conversations/{conversation_id}/messages", json={"videoUrl": video}, headers=headers
    )
    assert sent.status_code == 201
    assert _data(sent)["videoUrl"] == video


def test_an_oversized_or_unsized_video_upload_is_refused(db, client, auth_headers):
    headers = auth_headers(make_user(db))
    assert _presign(client, headers, "video/mp4", 100 * MB + 1).status_code == 413
    assert _presign(client, headers, "video/mp4").status_code == 400
    assert _presign(client, headers, "image/jpeg", 20 * MB + 1).status_code == 413
    assert _presign(client, headers, "video/mp4", 100 * MB).status_code == 200


def test_a_video_must_be_the_senders_own_upload(db, client, auth_headers):
    me, other, someone = make_user(db), make_user(db), make_user(db)
    theirs = _data(_presign(client, auth_headers(someone), "video/mp4", MB))["url"]

    response = client.post(
        "/api/conversations",
        json={"username": other.username, "videoUrl": theirs},
        headers=auth_headers(me),
    )
    assert response.status_code == 400


def test_a_photo_and_a_video_together_are_refused(db, client, auth_headers):
    me, other = make_user(db), make_user(db)
    headers = auth_headers(me)
    video = _data(_presign(client, headers, "video/mp4", MB))["url"]
    photo = _data(_presign(client, headers, "image/jpeg", MB))["url"]

    response = client.post(
        "/api/conversations",
        json={"username": other.username, "imageUrl": photo, "videoUrl": video},
        headers=headers,
    )
    assert response.status_code == 400
