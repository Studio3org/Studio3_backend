"""A profile's Collection (BACKEND_CHANGES B37), without a database: each piece once, newest
first; a tile carries the artist and never a price; a private account is refused to
strangers, a block hides it, and an unknown name is 404.

Runs with `pytest tests/unit/test_collection_tab_b37.py --noconftest`; the same over HTTP is
in tests/api/test_collection_tab_b37.py.
"""
import uuid
from types import SimpleNamespace

import pytest
from flask import Flask, g

from src.modules.user import collected
from src.shared.utils.app_error import AppError


def _piece(pid=None):
    return SimpleNamespace(id=pid or uuid.uuid4(), user_id=uuid.uuid4())


def test_each_piece_once_newest_first():
    a, b = _piece(), _piece()
    again = SimpleNamespace(id=a.id, user_id=a.user_id)
    assert collected.first_of_each([a, b, again]) == [a, b]


def test_a_tile_has_the_artist_and_no_price(monkeypatch):
    monkeypatch.setattr(
        collected,
        "piece_to_dict",
        lambda piece, media=None: {"id": str(piece.id), "title": "Night Walks", "priceCents": 350000, "currency": "usd"},
    )
    artist = SimpleNamespace(username="amara", name="Amara Osei", image="https://cdn.test/a.jpg")
    item = collected.to_item(_piece(), [], artist)
    assert item["title"] == "Night Walks"
    assert "priceCents" not in item and "currency" not in item
    assert item["author"] == {"username": "amara", "name": "Amara Osei", "profilePhotoUrl": "https://cdn.test/a.jpg"}
    assert collected.to_item(_piece(), [], None)["author"]["name"] is None


@pytest.fixture()
def listing(monkeypatch):
    owner = SimpleNamespace(id=uuid.uuid4(), username="kofi")
    state = SimpleNamespace(owner=owner, can_view=True, blocked=False, pieces=[])
    monkeypatch.setattr(collected, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(collected, "find_user_by_username", lambda db, name: state.owner if name == "kofi" else None)
    monkeypatch.setattr(collected.social_dao, "can_view_content", lambda db, user, viewer: state.can_view)
    monkeypatch.setattr(collected.block_dao, "is_blocked_either_way", lambda db, a, b: state.blocked)
    monkeypatch.setattr(collected, "collected_pieces", lambda db, buyer: state.pieces)
    return state


def _call(username, viewer=None):
    app = Flask(__name__)
    with app.test_request_context():
        g.user = {"id": str(viewer)} if viewer else None
        return collected.list_for_user(username)


def test_nothing_bought_is_an_empty_list(listing):
    assert _call("kofi") == ([], 200)
    assert _call("KOFI") == ([], 200)


def test_unknown_name_is_404(listing):
    with pytest.raises(AppError) as err:
        _call("nobody")
    assert err.value.status_code == 404


def test_private_is_refused_to_strangers(listing):
    listing.can_view = False
    with pytest.raises(AppError) as err:
        _call("kofi", uuid.uuid4())
    assert err.value.status_code == 403


def test_a_block_hides_it(listing):
    listing.blocked = True
    with pytest.raises(AppError) as err:
        _call("kofi", uuid.uuid4())
    assert err.value.status_code == 404
