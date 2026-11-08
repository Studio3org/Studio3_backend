"""A profile's Collection over HTTP (BACKEND_CHANGES B37): the pieces someone bought, newest
first, with the artist and no price; unpaid and cancelled orders don't count; a private
account's Collection is only for its followers.
"""
from tests.factories import make_order, make_piece, make_user


def test_the_pieces_they_bought(db, client):
    artist, collector = make_user(db, seller=True, name="Amara Osei"), make_user(db)
    older = make_piece(db, artist, title="Harbour")
    newer = make_piece(db, artist, title="Night Walks")
    unpaid = make_piece(db, artist, title="Still Waiting")
    cancelled = make_piece(db, artist, title="Called Off")
    make_order(db, buyer=collector, seller=artist, piece=older, status="completed")
    make_order(db, buyer=collector, seller=artist, piece=newer, status="paid")
    make_order(db, buyer=collector, seller=artist, piece=unpaid, status="pending_payment")
    make_order(db, buyer=collector, seller=artist, piece=cancelled, status="cancelled")
    db.commit()

    res = client.get(f"/api/users/{collector.username}/collection")
    assert res.status_code == 200, res.get_json()
    items = res.get_json()["data"]
    assert [i["title"] for i in items] == ["Night Walks", "Harbour"]
    assert items[0]["author"]["name"] == "Amara Osei"
    assert "priceCents" not in items[0]


def test_a_private_collection_is_for_followers(db, client, auth_headers):
    artist = make_user(db, seller=True)
    private = make_user(db, profile_visibility="private")
    make_order(db, buyer=private, seller=artist, piece=make_piece(db, artist), status="completed")
    stranger = make_user(db)
    db.commit()

    assert client.get(f"/api/users/{private.username}/collection").status_code == 403
    assert (
        client.get(f"/api/users/{private.username}/collection", headers=auth_headers(stranger)).status_code
        == 403
    )
    mine = client.get(f"/api/users/{private.username}/collection", headers=auth_headers(private))
    assert mine.status_code == 200
    assert len(mine.get_json()["data"]) == 1
