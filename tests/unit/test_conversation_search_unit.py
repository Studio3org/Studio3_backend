"""Search messages (BACKEND_CHANGES B10): the query, the preview and the endpoint's shape.

Runs without a database (`pytest tests/unit/test_conversation_search.py --noconftest`); the
same search against Postgres is in tests/api/test_conversation_search.py.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from flask import Flask, g
from sqlalchemy.dialects import postgresql

from src.modules.chat import chat_controller, chat_dao
from src.shared.utils.app_error import AppError

ME = uuid.uuid4()


# --- the preview -----------------------------------------------------------------------------

def test_a_match_near_the_start_shows_the_message_from_the_start():
    assert chat_dao.match_preview("Does it come framed?", "framed") == "Does it come framed?"


def test_a_match_further_in_starts_a_word_or_two_before_it():
    body = (
        "Thanks so much for getting back to me about the commission, I have been thinking "
        "about the size and I think the larger one framed in oak would suit the room best."
    )
    preview = chat_dao.match_preview(body, "FRAMED")
    assert preview.startswith("…")
    assert "framed in oak" in preview
    # Starts on a word, not in the middle of one.
    assert preview[1] != " " and body[body.index(preview[1:]) - 1] == " "
    assert len(preview) <= 140


def test_the_preview_is_one_line_and_at_most_140():
    preview = chat_dao.match_preview("first line\n\nsecond   line " + "word " * 60, "second")
    assert "\n" not in preview and "  " not in preview
    assert preview.startswith("first line second line")
    assert len(preview) <= 140


# --- the query -------------------------------------------------------------------------------

def test_wildcards_in_what_was_typed_are_taken_literally():
    assert chat_dao.contains_pattern("50%") == "%50\\%%"
    assert chat_dao.contains_pattern("a_b") == "%a\\_b%"
    assert chat_dao.contains_pattern("a\\b") == "%a\\\\b%"


def test_the_search_looks_at_names_handles_and_messages_in_your_own_inbox():
    sql = str(
        chat_dao.search_inbox_query(ME, "framed").compile(dialect=postgresql.dialect())
    )
    assert "users.name ILIKE" in sql
    assert "users.username ILIKE" in sql
    assert "chat_messages.body ILIKE" in sql
    # Only conversations the viewer is in, and only the inbox's (not requests sent to them).
    assert "conversations.participant_one_id = " in sql
    assert "conversations.participant_two_id = " in sql
    assert "conversations.status = " in sql


# --- the endpoint ----------------------------------------------------------------------------

def _conversation(minutes_ago):
    return SimpleNamespace(
        id=uuid.uuid4(),
        last_message_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


@pytest.fixture
def search(monkeypatch):
    """Calls `search_inbox` with `found` as what the database matched."""
    calls = []

    def run(args, found=(), matches=None):
        matches = matches or {}

        def search_inbox(_db, user_id, query, limit, before):
            calls.append({"query": query, "limit": limit, "before": before})
            return list(found)[:limit]

        monkeypatch.setattr(chat_controller, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
        monkeypatch.setattr(chat_dao, "search_inbox", search_inbox)
        monkeypatch.setattr(
            chat_dao, "newest_match",
            lambda _db, conversation_id, _q: matches.get(conversation_id),
        )
        monkeypatch.setattr(
            chat_controller, "_inbox_item",
            lambda _db, conversation, _me: {"id": str(conversation.id), "preview": "last"},
        )
        with Flask(__name__).test_request_context(query_string=args):
            g.user = {"id": str(ME)}
            data, status = chat_controller.search_inbox()
        assert status == 200
        return data

    run.calls = calls
    return run


def test_a_message_match_comes_with_its_preview_and_a_name_match_without(search):
    by_message, by_name = _conversation(1), _conversation(2)
    data = search(
        {"q": " framed "},
        found=[by_message, by_name],
        matches={by_message.id: SimpleNamespace(body="Yes, it ships framed.")},
    )
    assert search.calls[0]["query"] == "framed"
    assert [item["id"] for item in data["items"]] == [str(by_message.id), str(by_name.id)]
    assert data["items"][0]["matchPreview"] == "Yes, it ships framed."
    assert data["items"][1]["matchPreview"] is None
    assert data["items"][1]["preview"] == "last"
    assert data["nextCursor"] is None


def test_results_page_like_the_inbox(search):
    found = [_conversation(i) for i in range(3)]
    first = search({"q": "a", "limit": "2"}, found=found)
    assert len(first["items"]) == 2
    assert first["nextCursor"] == chat_controller._encode_cursor(found[1].last_message_at, found[1].id)
    search({"q": "a", "limit": "2", "cursor": first["nextCursor"]}, found=found[2:])
    assert search.calls[1]["before"] == (found[1].last_message_at, found[1].id)


def test_an_empty_search_finds_nothing_without_asking_the_database(search):
    assert search({"q": "   "}) == {"items": [], "nextCursor": None}
    assert search.calls == []


def test_a_very_long_search_is_refused(search):
    with pytest.raises(AppError):
        search({"q": "x" * 101})
