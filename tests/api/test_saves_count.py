"""The save count on your own piece, through the API (BACKEND_CHANGES B17).

`ListOwnerPiece` "Saved by [N] people", `ListDone` "We let the [N] people who saved it know".
"""
import uuid

from src.shared.models.social import Collection, CollectionItem, Save
from tests.factories import make_piece, make_user


def _save(db, user, piece):
    db.add(Save(id=uuid.uuid4(), user_id=user.id, target_type="piece", target_id=piece.id))
    db.commit()


def test_the_owner_sees_the_count_and_nobody_else_gets_it(db, client, auth_headers):
    artist = make_user(db, seller=True)
    piece = make_piece(db, artist, is_for_sale=False)
    quick, both, shelf_only = make_user(db), make_user(db), make_user(db)
    _save(db, quick, piece)
    _save(db, both, piece)
    for user in (both, shelf_only):
        shelf = Collection(id=uuid.uuid4(), user_id=user.id, name="Wishlist")
        db.add(shelf)
        db.flush()
        db.add(CollectionItem(id=uuid.uuid4(), collection_id=shelf.id, target_type="piece", target_id=piece.id))
    db.commit()

    mine = client.get(f"/api/pieces/{piece.id}", headers=auth_headers(artist))
    # Three people, each once, though one saved it twice.
    assert mine.get_json()["data"]["savesCount"] == 3

    theirs = client.get(f"/api/pieces/{piece.id}", headers=auth_headers(quick))
    signed_out = client.get(f"/api/pieces/{piece.id}")
    assert "savesCount" not in theirs.get_json()["data"]
    assert "savesCount" not in signed_out.get_json()["data"]


def test_listing_it_returns_the_count(db, client, auth_headers):
    artist = make_user(db, seller=True)
    piece = make_piece(db, artist, is_for_sale=False)
    _save(db, make_user(db), piece)

    listed = client.patch(
        f"/api/pieces/{piece.id}",
        json={"isForSale": True, "listingType": "fixed", "priceCents": 95000},
        headers=auth_headers(artist),
    )

    assert listed.status_code == 200, listed.get_json()
    assert listed.get_json()["data"]["savesCount"] == 1
