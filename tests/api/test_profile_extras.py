"""Profile extras over HTTP: mediums under the username (BACKEND_CHANGES B31), Private
Collection (B34), a private profile's header (B36) and the artists count.
"""
from tests.factories import make_order, make_piece, make_user


def test_mediums_round_trip_and_a_fourth_is_refused(db, client, auth_headers):
    artist = make_user(db, seller=True)
    db.commit()
    headers = auth_headers(artist)

    saved = client.patch("/api/user/me", json={"profileMediums": ["Painting", "Mixed media"]}, headers=headers)
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()["data"]["profileMediums"] == ["Painting", "Mixed media"]
    public = client.get(f"/api/user/{artist.username}").get_json()["data"]
    assert public["profileMediums"] == ["Painting", "Mixed media"]

    refused = client.patch("/api/user/me", json={"profileMediums": ["Oil", "Ink", "Clay", "Glass"]}, headers=headers)
    assert refused.status_code == 400


def test_a_private_profile_keeps_its_header(db, client, auth_headers):
    artist = make_user(db, seller=True)
    private = make_user(db, profile_visibility="private", bio="Collecting work from Dallas artists.")
    stranger = make_user(db)
    make_order(db, buyer=private, seller=artist, piece=make_piece(db, artist), status="completed")
    db.commit()

    header = client.get(f"/api/user/{private.username}", headers=auth_headers(stranger)).get_json()["data"]
    assert header["bio"] == "Collecting work from Dallas artists."
    assert header["collectedCount"] == 1
    assert header["artistsCount"] == 1
    assert "followingCount" in header and "savesCount" in header and "messagePermission" in header
    assert "followersCount" not in header and "piecesCount" not in header


def test_a_private_collection_is_hidden_from_followers_too(db, client, auth_headers):
    artist, owner, fan = make_user(db, seller=True), make_user(db), make_user(db)
    make_order(db, buyer=owner, seller=artist, piece=make_piece(db, artist), status="completed")
    db.commit()

    turned = client.patch("/api/user/me", json={"collectionPrivate": True}, headers=auth_headers(owner))
    assert turned.get_json()["data"]["collectionPrivate"] is True

    assert client.get(f"/api/users/{owner.username}/collection", headers=auth_headers(fan)).get_json()["data"] == []
    mine = client.get(f"/api/users/{owner.username}/collection", headers=auth_headers(owner)).get_json()["data"]
    assert len(mine) == 1
