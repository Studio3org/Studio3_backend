"""Saved pieces and artists you follow, through the API (BACKEND_CHANGES B12).

Savers hear when a piece goes on sale or changes price; followers hear when an artist posts.
One row a day each; the author is never told; a type turned off still gets its row, no push.
"""
import uuid

import pytest

from src.modules.notifications import notifications_dao
from src.shared.models.notification import Notification
from src.shared.models.social import Collection, CollectionItem, Follow, Save
from tests.factories import make_piece, make_user


@pytest.fixture
def pushed(monkeypatch):
    calls = []

    def _fake_send(user_id, title, body, data=None):
        calls.append({"userId": user_id, "title": title, "body": body, "data": data})
        return True

    monkeypatch.setattr(notifications_dao, "send_push", _fake_send)
    return calls


def _rows(db, user, type):
    db.expire_all()
    return db.query(Notification).filter(Notification.user_id == user.id, Notification.type == type).all()


def _upload(client, headers, purpose):
    response = client.post(
        "/api/media/presign", json={"purpose": purpose, "contentType": "image/jpeg"}, headers=headers
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]["url"]


def _follow(db, follower, artist, status="accepted"):
    db.add(Follow(id=uuid.uuid4(), follower_id=follower.id, following_id=artist.id, status=status))
    db.commit()


def test_savers_hear_it_went_on_sale_then_its_price_once_a_day(db, client, auth_headers, pushed):
    artist = make_user(db, seller=True)
    piece = make_piece(db, artist, is_for_sale=False, title="Paper Planes III")
    quick = make_user(db)
    db.add(Save(id=uuid.uuid4(), user_id=quick.id, target_type="piece", target_id=piece.id))
    collector = make_user(db)
    shelf = Collection(id=uuid.uuid4(), user_id=collector.id, name="Wishlist")
    db.add(shelf)
    db.flush()
    db.add(CollectionItem(id=uuid.uuid4(), collection_id=shelf.id, target_type="piece", target_id=piece.id))
    db.commit()
    headers = auth_headers(artist)

    listed = client.patch(
        f"/api/pieces/{piece.id}",
        json={"isForSale": True, "listingType": "fixed", "priceCents": 95000},
        headers=headers,
    )
    assert listed.status_code == 200, listed.get_json()
    for saver in (quick, collector):
        (row,) = _rows(db, saver, "saved_piece_listed")
        assert row.payload["message"] == "Paper Planes III, which you saved, is now available for $950."
        assert row.target_id == piece.id

    for price in (85000, 80000):
        assert client.patch(f"/api/pieces/{piece.id}", json={"priceCents": price}, headers=headers).status_code == 200
    (row,) = _rows(db, quick, "saved_piece_price")
    assert row.payload["message"] == "Paper Planes III, which you saved, is now $800 (was $950)."

    # One push each for listed and the first price change; the second change only updated.
    assert sorted(p["userId"] for p in pushed) == sorted([quick.id, collector.id] * 2)
    assert _rows(db, artist, "saved_piece_listed") == []


def test_followers_hear_about_new_work_once_a_day(db, client, auth_headers, pushed):
    artist = make_user(db, name="Amara Osei")
    follower, requester, stranger = make_user(db), make_user(db), make_user(db)
    _follow(db, follower, artist)
    _follow(db, requester, artist, status="pending")
    headers = auth_headers(artist)

    piece = client.post(
        "/api/pieces", json={"mediaUrl": _upload(client, headers, "piece"), "title": "Tide"}, headers=headers
    )
    assert piece.status_code == 201, piece.get_json()
    scene = client.post("/api/posts", json={"mediaUrl": _upload(client, headers, "post")}, headers=headers)
    assert scene.status_code == 201, scene.get_json()

    (row,) = _rows(db, follower, "followed_artist_post")
    assert row.payload["message"] == "Amara Osei posted a new piece and a new Scene."
    assert (row.target_type, str(row.target_id)) == ("post", scene.get_json()["data"]["id"])
    assert [p["userId"] for p in pushed] == [follower.id]
    for nobody in (requester, stranger, artist):
        assert _rows(db, nobody, "followed_artist_post") == []


def test_a_draft_tells_nobody_until_it_goes_up(db, client, auth_headers, pushed):
    artist = make_user(db)
    follower = make_user(db)
    _follow(db, follower, artist)
    headers = auth_headers(artist)

    draft = client.post(
        "/api/pieces",
        json={"mediaUrl": _upload(client, headers, "piece"), "title": "Tide", "status": "draft"},
        headers=headers,
    )
    assert draft.status_code == 201
    assert _rows(db, follower, "followed_artist_post") == []

    live = client.patch(f"/api/pieces/{draft.get_json()['data']['id']}", json={"status": "live"}, headers=headers)
    assert live.status_code == 200, live.get_json()
    assert len(_rows(db, follower, "followed_artist_post")) == 1


def test_turned_off_means_the_row_but_no_push(db, client, auth_headers, pushed):
    artist = make_user(db)
    follower = make_user(db, notification_preferences={"push": {"followed_artist_post": False}})
    _follow(db, follower, artist)
    headers = auth_headers(artist)

    assert client.post(
        "/api/posts", json={"mediaUrl": _upload(client, headers, "post")}, headers=headers
    ).status_code == 201
    assert len(_rows(db, follower, "followed_artist_post")) == 1
    assert pushed == []
