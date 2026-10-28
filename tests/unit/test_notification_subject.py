"""What a notification is about: the piece's title and picture in its payload (BACKEND_CHANGES B6).

Runs without a database (`pytest tests/unit/test_notification_subject.py --noconftest`): a small
stand-in session answers `get` by model and id, and `query(...).first()` from a list per model.
"""
import uuid
from types import SimpleNamespace

from src.modules.notifications import notifications_dao
from src.modules.notifications.notifications_dao import _subject_fields, create_notification
from src.shared.models.inquiry import Inquiry
from src.shared.models.order import OrderItem
from src.shared.models.piece import Piece
from src.shared.models.piece_media import PieceMedia
from src.shared.models.post import Post

PIECE_ID = uuid.uuid4()
VIDEO_PIECE_ID = uuid.uuid4()
BARE_VIDEO_PIECE_ID = uuid.uuid4()
POST_ID = uuid.uuid4()
VIDEO_POST_ID = uuid.uuid4()
ORDER_ID = uuid.uuid4()
INQUIRY_ID = uuid.uuid4()


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_):
        return self

    def order_by(self, *_):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, rows=None, first=None):
        self.rows = rows or {}
        self.first_rows = first or {}
        self.added = []

    def get(self, model, key):
        return self.rows.get((model, key))

    def query(self, model):
        return _Query(self.first_rows.get(model, []))

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        pass

    def refresh(self, _):
        pass


def _session():
    pieces = {
        PIECE_ID: SimpleNamespace(id=PIECE_ID, title="Paper Planes III", media_type="image",
                                  media_url="https://cdn.test/planes.png"),
        VIDEO_PIECE_ID: SimpleNamespace(id=VIDEO_PIECE_ID, title="Tide", media_type="video",
                                        media_url="https://cdn.test/tide.mp4"),
    }
    rows = {(Piece, k): v for k, v in pieces.items()}
    rows[(Post, POST_ID)] = SimpleNamespace(
        id=POST_ID, linked_piece_id=PIECE_ID, media_type="video",
        media_url="https://cdn.test/scene.mp4", thumbnail_url="https://cdn.test/scene.jpg",
    )
    rows[(Post, VIDEO_POST_ID)] = SimpleNamespace(
        id=VIDEO_POST_ID, linked_piece_id=None, media_type="video",
        media_url="https://cdn.test/other.mp4", thumbnail_url=None,
    )
    rows[(Inquiry, INQUIRY_ID)] = SimpleNamespace(id=INQUIRY_ID, piece_id=PIECE_ID)
    return _Session(
        rows=rows,
        first={
            OrderItem: [SimpleNamespace(order_id=ORDER_ID, piece_id=PIECE_ID)],
            PieceMedia: [SimpleNamespace(media_url="https://cdn.test/tide-still.jpg")],
        },
    )


PLANES = {
    "pieceId": str(PIECE_ID),
    "pieceTitle": "Paper Planes III",
    "pieceImageUrl": "https://cdn.test/planes.png",
}


def test_a_piece_target_names_and_shows_the_piece():
    assert _subject_fields(_session(), "piece", PIECE_ID) == PLANES


def test_a_video_piece_shows_its_first_photo():
    assert _subject_fields(_session(), "piece", VIDEO_PIECE_ID) == {
        "pieceId": str(VIDEO_PIECE_ID),
        "pieceTitle": "Tide",
        "pieceImageUrl": "https://cdn.test/tide-still.jpg",
    }


def test_a_video_piece_without_a_photo_has_no_picture():
    db = _session()
    db.first_rows[PieceMedia] = []
    fields = _subject_fields(db, "piece", VIDEO_PIECE_ID)
    assert fields == {"pieceId": str(VIDEO_PIECE_ID), "pieceTitle": "Tide"}


def test_a_scene_gives_its_picture_and_its_linked_piece():
    assert _subject_fields(_session(), "post", POST_ID) == {
        **PLANES,
        "postImageUrl": "https://cdn.test/scene.jpg",
    }


def test_a_video_scene_without_a_thumbnail_or_piece_adds_nothing():
    assert _subject_fields(_session(), "post", VIDEO_POST_ID) == {}


def test_an_order_and_an_inquiry_give_their_piece():
    assert _subject_fields(_session(), "order", ORDER_ID) == PLANES
    assert _subject_fields(_session(), "inquiry", INQUIRY_ID) == PLANES


def test_other_targets_and_missing_rows_add_nothing():
    db = _session()
    assert _subject_fields(db, "user", uuid.uuid4()) == {}
    assert _subject_fields(db, "event", uuid.uuid4()) == {}
    assert _subject_fields(db, None, None) == {}
    assert _subject_fields(db, "piece", uuid.uuid4()) == {}
    db.first_rows[OrderItem] = []
    assert _subject_fields(db, "order", ORDER_ID) == {}


def test_the_stored_payload_has_the_piece_and_the_callers_values_win():
    db = _session()
    row = create_notification(
        db, uuid.uuid4(), "purchase", target_type="order", target_id=ORDER_ID,
        payload={"pieceTitle": "As sold", "totalCents": 350000},
    )
    assert row.payload == {
        "pieceId": str(PIECE_ID),
        "pieceTitle": "As sold",
        "pieceImageUrl": "https://cdn.test/planes.png",
        "totalCents": 350000,
    }


def test_create_and_push_stores_the_piece_with_the_message(monkeypatch):
    db = _session()
    monkeypatch.setattr(notifications_dao, "send_push", lambda *a, **k: None)
    row = notifications_dao.create_and_push(
        db, uuid.uuid4(), "like", "New like", "Amara liked your piece.",
        target_type="piece", target_id=PIECE_ID,
    )
    assert row.payload == {**PLANES, "message": "Amara liked your piece."}


def test_a_notification_about_nothing_keeps_its_payload():
    row = create_notification(_session(), uuid.uuid4(), "follow", target_type="user", target_id=uuid.uuid4())
    assert row.payload is None
