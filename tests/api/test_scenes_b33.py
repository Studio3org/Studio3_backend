"""Scenes and drafts over HTTP (BACKEND_CHANGES B33).

The save count moves with save and unsave; only the comment's writer and the Scene's artist
can delete it; comments off refuses new ones; a photo Scene keeps its photos; the detail
carries its linked pieces; drafts list both kinds with a failed publish; a posted Scene has a
share page and a draft doesn't.
"""
from tests.factories import make_piece, make_user


def _upload(client, headers):
    response = client.post(
        "/api/media/presign", json={"purpose": "post", "contentType": "image/jpeg"}, headers=headers
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]["url"]


def _scene(client, headers, **body):
    response = client.post("/api/posts", json={"mediaUrl": _upload(client, headers), **body}, headers=headers)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


def test_the_save_count_moves_with_save_and_unsave(db, client, auth_headers):
    artist, fan = make_user(db, seller=True), make_user(db)
    scene = _scene(client, auth_headers(artist))
    fan_headers = auth_headers(fan)

    detail = client.get(f"/api/posts/{scene['id']}", headers=fan_headers).get_json()["data"]
    assert (detail["saveCount"], detail["isSaved"]) == (0, False)

    client.post(f"/api/posts/{scene['id']}/save", headers=fan_headers)
    detail = client.get(f"/api/posts/{scene['id']}", headers=fan_headers).get_json()["data"]
    assert (detail["saveCount"], detail["isSaved"]) == (1, True)
    listed = client.get(f"/api/users/{artist.username}/posts", headers=fan_headers).get_json()["data"]
    assert (listed[0]["saveCount"], listed[0]["isSaved"]) == (1, True)

    client.delete(f"/api/posts/{scene['id']}/save", headers=fan_headers)
    detail = client.get(f"/api/posts/{scene['id']}", headers=fan_headers).get_json()["data"]
    assert (detail["saveCount"], detail["isSaved"]) == (0, False)


def test_only_the_writer_and_the_artist_can_delete_a_comment(db, client, auth_headers):
    artist, writer, stranger = make_user(db, seller=True), make_user(db), make_user(db)
    scene = _scene(client, auth_headers(artist))

    def comment():
        response = client.post(
            f"/api/posts/{scene['id']}/comments", json={"body": "Lovely light"}, headers=auth_headers(writer)
        )
        assert response.status_code == 201, response.get_json()
        return response.get_json()["data"]["id"]

    first, second = comment(), comment()
    assert client.delete(f"/api/comments/{first}", headers=auth_headers(stranger)).status_code == 404

    by_writer = client.delete(f"/api/comments/{first}", headers=auth_headers(writer))
    assert by_writer.status_code == 200
    assert by_writer.get_json()["data"]["commentCount"] == 1

    by_artist = client.delete(f"/api/comments/{second}", headers=auth_headers(artist))
    assert by_artist.status_code == 200
    listed = client.get(f"/api/posts/{scene['id']}/comments").get_json()["data"]["items"]
    assert listed == []


def test_comments_off_refuses_new_ones_until_turned_back_on(db, client, auth_headers):
    artist, fan = make_user(db, seller=True), make_user(db)
    headers = auth_headers(artist)
    scene = _scene(client, headers)

    off = client.patch(f"/api/posts/{scene['id']}", json={"commentsOff": True}, headers=headers)
    assert off.get_json()["data"]["commentsOff"] is True
    refused = client.post(
        f"/api/posts/{scene['id']}/comments", json={"body": "Hello"}, headers=auth_headers(fan)
    )
    assert refused.status_code == 403

    client.patch(f"/api/posts/{scene['id']}", json={"commentsOff": False}, headers=headers)
    allowed = client.post(
        f"/api/posts/{scene['id']}/comments", json={"body": "Hello"}, headers=auth_headers(fan)
    )
    assert allowed.status_code == 201


def test_a_photo_scene_keeps_its_photos_and_its_linked_pieces(db, client, auth_headers):
    artist = make_user(db, seller=True)
    headers = auth_headers(artist)
    first, second = make_piece(db, artist, title="First"), make_piece(db, artist, title="Second")
    photos = [_upload(client, headers) for _ in range(3)]

    response = client.post(
        "/api/posts",
        json={"mediaUrls": photos, "linkedPieceIds": [str(second.id), str(first.id)]},
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    scene = response.get_json()["data"]
    assert scene["mediaUrls"] == photos and scene["mediaUrl"] == photos[0]

    detail = client.get(f"/api/posts/{scene['id']}").get_json()["data"]
    assert detail["mediaUrls"] == photos
    assert [p["title"] for p in detail["linkedPieces"]] == ["Second", "First"]
    assert detail["linkedPieces"][0]["priceCents"] == second.price_cents


def test_drafts_list_both_kinds_and_a_failed_publish(db, client, auth_headers):
    artist = make_user(db, seller=True)
    headers = auth_headers(artist)
    piece = make_piece(db, artist, status="draft", is_for_sale=False)
    scene = _scene(client, headers, status="draft", draftStep="caption")

    failed = client.patch(f"/api/posts/{scene['id']}", json={"publishFailed": True}, headers=headers)
    assert failed.status_code == 200, failed.get_json()

    items = client.get("/api/user/me/drafts", headers=headers).get_json()["data"]["items"]
    assert [(d["type"], d["id"]) for d in items] == [("post", scene["id"]), ("piece", str(piece.id))]
    assert items[0]["publishFailed"] is True and items[0]["draftStep"] == "caption"
    assert "updatedAt" in items[1]

    live = client.patch(f"/api/posts/{scene['id']}", json={"status": "live"}, headers=headers)
    assert live.get_json()["data"]["publishFailed"] is False
    items = client.get("/api/user/me/drafts", headers=headers).get_json()["data"]["items"]
    assert [d["type"] for d in items] == ["piece"]


def test_a_posted_scene_has_a_share_page_and_a_draft_does_not(db, client, auth_headers):
    artist = make_user(db, seller=True, name="Mara Ellis")
    headers = auth_headers(artist)
    posted = _scene(client, headers, caption="Studio, late night")
    draft = _scene(client, headers, status="draft")
    crawler = {"User-Agent": "WhatsApp/2.0"}

    page = client.get(f"/share/post/{posted['id']}", headers=crawler)
    assert page.status_code == 200
    assert "A Scene by Mara Ellis" in page.get_data(as_text=True)
    assert client.get(f"/share/post/{draft['id']}", headers=crawler).status_code == 404
