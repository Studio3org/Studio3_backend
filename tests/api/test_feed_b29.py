"""The home feed (BACKEND_CHANGES B29): every photo of a piece, its series, For You ranked for the
viewer with a cursor that holds across ranks, and the Available filter.
"""
import uuid
from datetime import datetime, timedelta, timezone

from src.modules.pieces.pieces_dao import replace_piece_media
from src.shared.models.series import Series, SeriesPiece
from src.shared.models.social import Follow, Like
from tests.factories import make_piece, make_user


def _aged(db, piece, days):
    piece.created_at = datetime.now(timezone.utc) - timedelta(days=days)
    db.commit()
    return piece


def _items(client, url, headers=None):
    body = client.get(url, headers=headers).get_json()["data"]
    return body["items"], body.get("nextCursor")


def test_three_photos_come_back_cover_first_and_a_series_is_on_its_pieces(db, client):
    artist = make_user(db, seller=True)
    gallery = make_piece(db, artist, title="Three photos")
    replace_piece_media(db, gallery.id, [
        {"mediaUrl": "https://cdn.test/cover.jpg"},
        {"mediaUrl": "https://cdn.test/2.jpg"},
        {"mediaUrl": "https://cdn.test/3.jpg"},
    ])
    make_piece(db, artist, title="Alone")
    one = make_piece(db, artist, title="Paper Planes I")
    two = make_piece(db, artist, title="Paper Planes II", status="sold")
    series = Series(id=uuid.uuid4(), user_id=artist.id, name="Paper Planes")
    db.add(series)
    db.flush()
    for position, piece in enumerate((one, two)):
        db.add(SeriesPiece(id=uuid.uuid4(), series_id=series.id, piece_id=piece.id, position=position))
    db.commit()

    items, _ = _items(client, "/api/feed/for-you?limit=50")
    by_title = {i["title"]: i for i in items if i["type"] == "piece"}

    assert [img["mediaUrl"] for img in by_title["Three photos"]["images"]] == [
        "https://cdn.test/cover.jpg", "https://cdn.test/2.jpg", "https://cdn.test/3.jpg",
    ]
    assert "series" not in by_title["Alone"]
    shown = by_title["Paper Planes II"]["series"]
    assert shown["name"] == "Paper Planes"
    assert shown["pieceIds"] == [str(one.id), str(two.id)]
    assert [(p["title"], p["listingState"]) for p in shown["previewPieces"]] == [
        ("Paper Planes I", "available"), ("Paper Planes II", "collected"),
    ]
    assert by_title["Paper Planes I"]["series"]["id"] == str(series.id)


def test_available_never_returns_collected_or_ended(db, client):
    artist = make_user(db, seller=True)
    make_piece(db, artist, title="For sale")
    make_piece(db, artist, title="At auction", listing_type="auction")
    make_piece(db, artist, title="Sold", status="sold")
    make_piece(db, artist, title="Won", status="auction_won", listing_type="auction")
    make_piece(db, artist, title="Not for sale", is_for_sale=False, price_cents=None)

    items, _ = _items(client, "/api/feed/for-you?available=1&limit=50")

    assert {i["title"] for i in items} == {"For sale", "At auction"}
    assert {i["listingState"] for i in items} <= {"available", "auction_live"}
    assert all(i["type"] == "piece" for i in items)


def test_for_you_ranks_follows_then_taste_and_activity_then_the_rest(db, client, auth_headers):
    viewer = make_user(db, taste_preferences={"mediums": ["Painting"], "styles": [], "themes": []})
    friend, liked, stranger = (make_user(db, seller=True) for _ in range(3))
    db.add(Follow(id=uuid.uuid4(), follower_id=viewer.id, following_id=friend.id, status="accepted"))
    db.commit()
    newest = _aged(db, make_piece(db, stranger, title="Stranger, newest", medium="Ink"), 0)
    taste = _aged(db, make_piece(db, stranger, title="Painting", medium="painting"), 3)
    liked_piece = _aged(db, make_piece(db, liked, title="By someone liked", medium="Ink"), 5)
    db.add(Like(id=uuid.uuid4(), user_id=viewer.id, target_type="piece", target_id=liked_piece.id))
    db.commit()
    followed = _aged(db, make_piece(db, friend, title="Friend, old", medium="Ink"), 30)

    signed_in, _ = _items(client, "/api/feed/for-you?limit=50", auth_headers(viewer))
    guest, _ = _items(client, "/api/feed/for-you?limit=50")

    order = [i["title"] for i in signed_in if i["type"] == "piece"]
    assert order.index(followed.title) < order.index(taste.title)
    assert order.index(taste.title) < order.index(newest.title)
    assert order.index(liked_piece.title) < order.index(newest.title)
    guest_order = [i["title"] for i in guest if i["type"] == "piece"]
    assert guest_order.index(newest.title) < guest_order.index(followed.title), "a guest gets newest first"


def test_the_cursor_holds_across_ranks(db, client, auth_headers):
    viewer = make_user(db)
    friend, stranger = make_user(db, seller=True), make_user(db, seller=True)
    db.add(Follow(id=uuid.uuid4(), follower_id=viewer.id, following_id=friend.id, status="accepted"))
    db.commit()
    for i in range(4):
        _aged(db, make_piece(db, friend, title=f"Friend {i}"), 10 + i)
        _aged(db, make_piece(db, stranger, title=f"Stranger {i}"), i)
    headers = auth_headers(viewer)

    seen, cursor = [], None
    for _ in range(10):
        url = "/api/feed/for-you?limit=3" + (f"&cursor={cursor}" if cursor else "")
        items, cursor = _items(client, url, headers)
        seen += [i["title"] for i in items if i["type"] == "piece"]
        if not cursor:
            break

    assert seen == [f"Friend {i}" for i in range(4)] + [f"Stranger {i}" for i in range(4)]
