"""The development media store behind /api/media/local (SEC-2).

It stands in for S3 when no bucket is configured, so it is authorised the way S3 is: by the
signed URL /presign hands out, never by being reachable. It does not exist outside
development and testing, and it only ever serves images and video.
"""
from urllib.parse import urlsplit

import pytest

from src.shared.storage import local_storage
from tests.factories import make_user


@pytest.fixture(autouse=True)
def _isolated_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(local_storage, "UPLOAD_DIR", tmp_path)


def _presigned(client, headers, content_type="image/jpeg", length=None) -> str:
    body = {"purpose": "post", "contentType": content_type}
    if length is not None:
        body["contentLength"] = length
    response = client.post("/api/media/presign", json=body, headers=headers)
    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["devMode"] is True
    return data["presignedPutUrl"]


def _path(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}" if parts.query else parts.path


def test_a_presigned_upload_round_trips(db, client, auth_headers):
    headers = auth_headers(make_user(db))
    url = _path(_presigned(client, headers))

    put = client.put(url, data=b"\xff\xd8jpeg", content_type="image/jpeg")
    assert put.status_code == 204

    get = client.get(url.split("?")[0])
    assert get.status_code == 200
    assert get.data == b"\xff\xd8jpeg"
    assert get.headers["X-Content-Type-Options"] == "nosniff"


def test_an_unsigned_upload_is_refused(db, client, auth_headers):
    url = _path(_presigned(client, auth_headers(make_user(db))))

    response = client.put(url.split("?")[0], data=b"x", content_type="image/jpeg")

    assert response.status_code == 403


def test_a_signature_cannot_be_moved_to_another_users_key(db, client, auth_headers):
    mine = _path(_presigned(client, auth_headers(make_user(db))))
    victim = make_user(db)
    path, query = mine.split("?")
    their_key = path.replace(path.split("/")[4], victim.username.lower(), 1)

    response = client.put(f"{their_key}?{query}", data=b"x", content_type="image/jpeg")

    assert response.status_code == 403


def test_the_signed_content_type_and_size_are_enforced(db, client, auth_headers):
    headers = auth_headers(make_user(db))
    url = _path(_presigned(client, headers, length=4))

    assert client.put(url, data=b"<html>", content_type="text/html").status_code == 403
    assert client.put(url, data=b"12345", content_type="image/jpeg").status_code == 403
    assert client.put(url, data=b"1234", content_type="image/jpeg").status_code == 204


def test_an_expired_upload_url_is_refused(db, client, auth_headers, monkeypatch):
    url = _path(_presigned(client, auth_headers(make_user(db))))
    later = local_storage.time.time() + local_storage.UPLOAD_URL_TTL_SECONDS + 1
    monkeypatch.setattr(local_storage.time, "time", lambda: later)

    response = client.put(url, data=b"x", content_type="image/jpeg")

    assert response.status_code == 403


def test_only_images_and_video_are_ever_served(client, tmp_path):
    (tmp_path / "someone").mkdir()
    (tmp_path / "someone" / "page.html").write_text("<script>steal()</script>")

    assert client.get("/api/media/local/someone/page.html").status_code == 404


def test_the_store_does_not_exist_outside_development(db, client, auth_headers, monkeypatch):
    url = _path(_presigned(client, auth_headers(make_user(db))))
    monkeypatch.setenv("FLASK_ENV", "production")

    assert client.put(url, data=b"x", content_type="image/jpeg").status_code == 404
    assert client.get(url.split("?")[0]).status_code == 404
