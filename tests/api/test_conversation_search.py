"""Search messages: GET /api/conversations/search (BACKEND_CHANGES B10).

Finds the viewer's own conversations by the other person's name or handle, or by any
message, case-insensitively; rows are the inbox's plus `matchPreview`.
"""
from src.modules.chat import chat_dao
from tests.factories import make_user


def _search(client, headers, q):
    response = client.get("/api/conversations/search", query_string={"q": q}, headers=headers)
    assert response.status_code == 200
    return response.get_json()["data"]["items"]


def _chat(db, starter, other, *bodies, status="open"):
    conversation = chat_dao.create_conversation(db, starter.id, other.id, status=status)
    for body in bodies:
        chat_dao.create_message(db, conversation, starter.id, body=body)
    return conversation


def test_finds_your_chats_by_message_text_with_the_newest_match(db, client, auth_headers):
    me = make_user(db, name="Jordan Lee")
    amara = make_user(db, name="Amara Osei")
    kofi = make_user(db, name="Kofi Mensah")
    framed = _chat(db, me, amara, "Does Bodies in Color come framed?", "Great, and is it FRAMED in oak?")
    _chat(db, me, kofi, "See you at the opening")
    # Someone else's chat with the same words never shows.
    _chat(db, kofi, amara, "The framed print arrived")

    items = _search(client, auth_headers(me), "framed")

    assert [item["id"] for item in items] == [str(framed.id)]
    assert items[0]["matchPreview"] == "Great, and is it FRAMED in oak?"
    assert items[0]["otherParty"]["name"] == "Amara Osei"


def test_finds_your_chats_by_name_or_handle(db, client, auth_headers):
    me = make_user(db)
    amara = make_user(db, name="Amara Osei", username="amarasart")
    chat = _chat(db, me, amara, "Hello")
    headers = auth_headers(me)

    by_name = _search(client, headers, "OSEI")
    assert [item["id"] for item in by_name] == [str(chat.id)]
    assert by_name[0]["matchPreview"] is None
    assert by_name[0]["preview"] == "Hello"

    assert [item["id"] for item in _search(client, headers, "amarasa")] == [str(chat.id)]


def test_requests_waiting_on_you_are_not_searched(db, client, auth_headers):
    """The Messages tab lists them behind "Message requests", not among the chats."""
    me = make_user(db)
    stranger = make_user(db)
    _chat(db, stranger, me, "Is the framed one still available?", status="pending")

    assert _search(client, auth_headers(me), "framed") == []


def test_wildcards_are_matched_literally(db, client, auth_headers):
    me = make_user(db)
    other = make_user(db)
    _chat(db, me, other, "Hello there")
    discounted = _chat(db, me, make_user(db), "Is 10% off still on?")
    headers = auth_headers(me)

    assert [item["id"] for item in _search(client, headers, "%")] == [str(discounted.id)]
    # As a wildcard, "_" would match the space in "Hello there".
    assert _search(client, headers, "o_t") == []


def test_needs_a_signed_in_user(client):
    assert client.get("/api/conversations/search?q=hi").status_code == 401
