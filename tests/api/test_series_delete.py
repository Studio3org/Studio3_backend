"""Delete a series through the API (BACKEND_CHANGES B15).

`NoteSeriesEdit`: "Delete series asks to confirm; the pieces stay on the profile, just not
grouped." Owner only; anyone else gets 404.
"""
from src.shared.models.piece import Piece
from src.shared.models.series import Series, SeriesPiece
from tests.factories import make_piece, make_user


def _make_series(client, headers, pieces):
    response = client.post(
        "/api/series",
        json={"name": "Bodies in Color", "pieceIds": [str(p.id) for p in pieces]},
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["id"]


def test_the_owner_deletes_it_and_the_pieces_stay(db, client, auth_headers):
    artist = make_user(db, seller=True)
    pieces = [make_piece(db, artist, title=f"Bodies in Color {n}") for n in (1, 2)]
    headers = auth_headers(artist)
    series_id = _make_series(client, headers, pieces)

    response = client.delete(f"/api/series/{series_id}", headers=headers)

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"] == {"id": series_id, "deleted": True}
    db.expire_all()
    assert db.query(Series).filter(Series.id == series_id).count() == 0
    assert db.query(SeriesPiece).filter(SeriesPiece.series_id == series_id).count() == 0
    # Still on the profile, just not grouped.
    for piece in pieces:
        assert db.get(Piece, piece.id).deleted_at is None
        detail = client.get(f"/api/pieces/{piece.id}", headers=headers)
        assert detail.status_code == 200
        assert detail.get_json()["data"]["series"] is None
    assert client.get(f"/api/series/{series_id}", headers=headers).status_code == 404
    assert client.get("/api/user/me/series", headers=headers).get_json()["data"] == []


def test_someone_else_gets_not_found(db, client, auth_headers):
    artist, other = make_user(db, seller=True), make_user(db)
    series_id = _make_series(client, auth_headers(artist), [make_piece(db, artist)])

    response = client.delete(f"/api/series/{series_id}", headers=auth_headers(other))

    assert response.status_code == 404
    db.expire_all()
    assert db.query(Series).filter(Series.id == series_id).count() == 1
