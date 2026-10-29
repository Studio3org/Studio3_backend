"""The piece a conversation started from (BACKEND_CHANGES B9).

Runs without a database (`pytest tests/unit/test_conversation_piece.py --noconftest`).
"""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from flask import Flask, g

from src.modules.chat import chat_controller, chat_dao
from src.shared.models.chat import Conversation
from src.shared.models.piece import Piece
from src.shared.models.user import User
from src.shared.utils.app_error import AppError

ME = SimpleNamespace(id=uuid.uuid4(), username="jordanlee", name="Jordan Lee")
ARTIST = SimpleNamespace(
    id=uuid.uuid4(), username="amarasart", name="Amara Osei", message_permission="everyone",
)
STRANGER = uuid.uuid4()


def _piece(owner=ARTIST.id, for_sale=True, deleted=False):
    return SimpleNamespace(
        id=uuid.uuid4(), user_id=owner, title="Bodies in Color", media_type="image",
        media_url="https://cdn.test/bodies.png", is_for_sale=for_sale, price_cents=350000,
        deleted_at=datetime.now(timezone.utc) if deleted else None,
    )


class _Session:
    def __init__(self, *rows):
        self.rows = {}
        for model, row in rows:
            self.rows[(model, row.id)] = row
        self.commits = 0

    def get(self, model, key):
        return self.rows.get((model, key))

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def _conversation(piece_id=None):
    now = datetime.now(timezone.utc)
    return Conversation(
        id=uuid.uuid4(), participant_one_id=ME.id, participant_two_id=ARTIST.id, status="open",
        last_message_at=now, created_at=now, piece_id=piece_id,
    )


# --- what the chat says about its piece ---------------------------------------------------

def test_the_summary_has_the_picture_the_price_and_the_artist():
    piece = _piece()
    db = _Session((Piece, piece), (User, ARTIST))
    assert chat_dao.piece_summary(db, _conversation(piece.id)) == {
        "id": str(piece.id),
        "title": "Bodies in Color",
        "imageUrl": "https://cdn.test/bodies.png",
        "priceCents": 350000,
        "artistUsername": "amarasart",
    }


def test_no_price_when_not_for_sale_and_nothing_when_gone():
    kept = _piece(for_sale=False)
    gone = _piece(deleted=True)
    db = _Session((Piece, kept), (Piece, gone), (User, ARTIST))
    assert chat_dao.piece_summary(db, _conversation(kept.id))["priceCents"] is None
    assert chat_dao.piece_summary(db, _conversation(gone.id)) is None
    assert chat_dao.piece_summary(db, _conversation(None)) is None
    assert chat_dao.piece_summary(db, _conversation(uuid.uuid4())) is None


def test_the_list_carries_the_piece():
    summary = {"id": "p1", "title": "Bodies in Color"}
    item = chat_dao.conversation_to_inbox_dict(_conversation(), ME.id, None, "Hi", piece=summary)
    assert item["piece"] == summary
    assert chat_dao.conversation_to_inbox_dict(_conversation(), ME.id, None, "Hi")["piece"] is None


def test_only_a_piece_of_one_of_the_two_people_is_taken():
    theirs, mine, someone_elses, gone = _piece(), _piece(owner=ME.id), _piece(owner=STRANGER), _piece(deleted=True)
    db = _Session((Piece, theirs), (Piece, mine), (Piece, someone_elses), (Piece, gone))
    assert chat_dao.piece_for_new_chat(db, theirs.id, ME.id, ARTIST.id) is theirs
    assert chat_dao.piece_for_new_chat(db, mine.id, ME.id, ARTIST.id) is mine
    assert chat_dao.piece_for_new_chat(db, someone_elses.id, ME.id, ARTIST.id) is None
    assert chat_dao.piece_for_new_chat(db, gone.id, ME.id, ARTIST.id) is None
    assert chat_dao.piece_for_new_chat(db, None, ME.id, ARTIST.id) is None


# --- starting a chat from a piece ---------------------------------------------------------

@pytest.fixture
def start(monkeypatch):
    """Calls `start_conversation` with the chat between ME and ARTIST as [existing]."""
    def run(body, existing=None, piece=None):
        rows = [(Piece, piece)] if piece else []
        db = _Session(*rows)
        created = []

        def create_conversation(_db, a, b, status="open"):
            conversation = _conversation()
            conversation.status = status
            created.append(conversation)
            return conversation

        monkeypatch.setattr(chat_controller, "SessionLocal", lambda: db)
        monkeypatch.setattr(chat_controller, "get_user_by_id", lambda _db, _id: ME)
        monkeypatch.setattr(chat_controller, "find_user_by_username", lambda _db, _u: ARTIST)
        monkeypatch.setattr(chat_controller.block_dao, "is_blocked_either_way", lambda *_a: False)
        monkeypatch.setattr(chat_controller.social_dao, "user_follows", lambda *_a: True)
        monkeypatch.setattr(chat_dao, "get_conversation_between", lambda *_a: existing)
        monkeypatch.setattr(chat_dao, "create_conversation", create_conversation)
        monkeypatch.setattr(chat_dao, "create_message", lambda *_a, **_k: SimpleNamespace())
        monkeypatch.setattr(chat_dao, "message_to_dict", lambda *_a: {})
        monkeypatch.setattr(chat_controller, "_notify_new_message", lambda *_a, **_k: None)
        monkeypatch.setattr(chat_controller, "_broadcast_new_message", lambda *_a: None)
        with Flask(__name__).test_request_context(json=body):
            g.user = {"id": str(ME.id)}
            chat_controller.start_conversation()
        return created[0] if created else existing

    return run


def test_a_new_chat_from_a_piece_remembers_it(start):
    piece = _piece()
    conversation = start({"username": "amarasart", "message": "Hi!", "pieceId": str(piece.id)}, piece=piece)
    assert conversation.piece_id == piece.id


def test_a_new_chat_from_anywhere_else_has_no_piece(start):
    assert start({"username": "amarasart", "message": "Hi!"}).piece_id is None


def test_a_chat_already_under_way_keeps_what_it_began_with(start):
    piece = _piece()
    existing = _conversation()
    start({"username": "amarasart", "message": "Hi!", "pieceId": str(piece.id)}, existing=existing, piece=piece)
    assert existing.piece_id is None


def test_a_closed_chat_started_again_from_a_piece_takes_it(start):
    piece = _piece()
    existing = _conversation()
    existing.status = "closed"
    start({"username": "amarasart", "message": "Hi!", "pieceId": str(piece.id)}, existing=existing, piece=piece)
    assert existing.piece_id == piece.id


def test_a_bad_piece_id_is_refused(start):
    with pytest.raises(AppError):
        start({"username": "amarasart", "message": "Hi!", "pieceId": "not-a-uuid"})
