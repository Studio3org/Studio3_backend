"""Mute a conversation (BACKEND_CHANGES B8): per person, no push, not in the unread badge.

Runs without a database (`pytest tests/unit/test_conversation_mute.py --noconftest`).
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from flask import Flask, g

from src.modules.chat import chat_controller, chat_dao
from src.shared.models.chat import Conversation
from src.shared.utils.app_error import AppError

ME = uuid.uuid4()
THEM = uuid.uuid4()


def _conversation(**overrides):
    now = datetime.now(timezone.utc)
    return Conversation(
        id=uuid.uuid4(),
        participant_one_id=ME,
        participant_two_id=THEM,
        status="open",
        participant_one_read_at=now - timedelta(hours=1),
        participant_two_read_at=None,
        last_message_at=now,
        created_at=now,
        **overrides,
    )


class _Session:
    def __init__(self, conversations=()):
        self.conversations = {c.id: c for c in conversations}
        self.commits = 0

    def get(self, _model, key):
        return self.conversations.get(key)

    def commit(self):
        self.commits += 1

    def execute(self, _statement):
        rows = list(self.conversations.values())
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    def close(self):
        pass


def test_mute_is_for_one_person_only():
    conversation = _conversation()
    db = _Session([conversation])
    assert chat_dao.set_muted(db, conversation, ME, True) is True
    assert chat_dao.is_muted_for(conversation, ME)
    assert not chat_dao.is_muted_for(conversation, THEM)
    assert chat_dao.set_muted(db, conversation, ME, False) is False
    assert not chat_dao.is_muted_for(conversation, ME)
    assert db.commits == 2


def test_the_list_says_muted_and_still_unread():
    conversation = _conversation()
    conversation.participant_one_muted_at = datetime.now(timezone.utc)
    item = chat_dao.conversation_to_inbox_dict(conversation, ME, None, "Hi", last_sender_id=THEM)
    assert item["muted"] is True
    assert item["unread"] is True
    other = chat_dao.conversation_to_inbox_dict(conversation, THEM, None, "Hi", last_sender_id=ME)
    assert other["muted"] is False


def test_a_muted_conversation_is_left_out_of_the_unread_badge(monkeypatch):
    loud, quiet = _conversation(), _conversation()
    quiet.participant_one_muted_at = datetime.now(timezone.utc)
    db = _Session([loud, quiet])
    monkeypatch.setattr(chat_dao, "list_messages", lambda *_a, **_k: [SimpleNamespace(sender_id=THEM)])
    assert chat_dao.count_unread_inbox(db, ME) == 1
    quiet.participant_one_muted_at = None
    assert chat_dao.count_unread_inbox(db, ME) == 2


def test_no_push_for_a_muted_recipient(monkeypatch):
    conversation = _conversation()
    db = _Session([conversation])
    pushes = []
    monkeypatch.setattr(chat_controller.notifications_dao, "push_only", lambda *a, **k: pushes.append(k))
    sender = SimpleNamespace(name="Amara Osei")
    chat_controller._notify_new_message(db, conversation.id, ME, sender, "Hi", is_new_thread=False)
    assert len(pushes) == 1
    conversation.participant_one_muted_at = datetime.now(timezone.utc)
    chat_controller._notify_new_message(db, conversation.id, ME, sender, "Hi again", is_new_thread=False)
    assert len(pushes) == 1
    # The other person never muted it: they still get theirs.
    chat_controller._notify_new_message(db, conversation.id, THEM, sender, "Hi", is_new_thread=False)
    assert len(pushes) == 2


@pytest.fixture
def app():
    return Flask(__name__)


def _patch(app, monkeypatch, conversation, body, user=ME):
    db = _Session([conversation])
    monkeypatch.setattr(chat_controller, "SessionLocal", lambda: db)
    with app.test_request_context(json=body):
        g.user = {"id": str(user)}
        return chat_controller.set_mute(str(conversation.id))


def test_patch_mute_sets_it_for_the_caller(app, monkeypatch):
    conversation = _conversation()
    assert _patch(app, monkeypatch, conversation, {"muted": True}) == ({"muted": True}, 200)
    assert chat_dao.is_muted_for(conversation, ME)
    assert _patch(app, monkeypatch, conversation, {"muted": False}) == ({"muted": False}, 200)


@pytest.mark.parametrize("body", [{}, {"muted": "yes"}, {"muted": 1}])
def test_patch_mute_needs_true_or_false(app, monkeypatch, body):
    with pytest.raises(AppError):
        _patch(app, monkeypatch, _conversation(), body)


def test_only_someone_in_the_conversation_can_mute_it(app, monkeypatch):
    with pytest.raises(AppError):
        _patch(app, monkeypatch, _conversation(), {"muted": True}, user=uuid.uuid4())
