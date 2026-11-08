"""Profile extras without a database: mediums under the username (BACKEND_CHANGES B31), the
Private Collection switch (B34) and a private profile's header (B36).

Runs with `pytest tests/unit/test_profile_extras.py --noconftest`; the same over HTTP is in
tests/api/test_profile_extras.py.
"""
import uuid
from types import SimpleNamespace

import pytest
from flask import Flask, g

from src.modules.user import collected, profile_extras as pe
from src.shared.utils.app_error import AppError


def test_mediums_keep_their_order_trimmed_without_repeats():
    assert pe.clean_profile_mediums(["  Painting ", "Mixed   media", "painting", ""]) == ["Painting", "Mixed media"]
    assert pe.clean_profile_mediums([]) == []
    assert pe.clean_profile_mediums(None) == []


@pytest.mark.parametrize(
    "value",
    [["Oil", "Ink", "Clay", "Glass"], "Painting", [1, 2], ["x" * 41]],
)
def test_mediums_refused(value):
    with pytest.raises(AppError) as err:
        pe.clean_profile_mediums(value)
    assert err.value.status_code == 400


def test_private_collection_is_a_boolean():
    assert pe.clean_collection_private(True) is True
    with pytest.raises(AppError):
        pe.clean_collection_private("yes")


def test_a_private_collection_is_the_owners_alone():
    owner = uuid.uuid4()
    private = SimpleNamespace(id=owner, collection_private=True)
    assert pe.collection_visible(private, owner)
    assert not pe.collection_visible(private, uuid.uuid4())
    assert not pe.collection_visible(private, None)
    assert pe.collection_visible(SimpleNamespace(id=owner, collection_private=False), None)


def test_the_locked_header_has_what_the_board_shows_and_never_the_gated_counts():
    user = SimpleNamespace(
        username="jordanlee", name="Jordan Lee", image="a.jpg", cover_photo_url="c.jpg",
        bio="Collecting work from Dallas artists.", role="collector", message_permission="following",
    )
    header = pe.locked_header(
        user, is_pending=True, following_count=14, collected_count=6, artists_count=4, saves_count=32,
    )
    assert header["followingCount"] == 14
    assert (header["collectedCount"], header["artistsCount"], header["savesCount"]) == (6, 4, 32)
    assert header["bio"] == "Collecting work from Dallas artists."
    assert header["coverPhotoUrl"] == "c.jpg"
    assert header["messagePermission"] == "following"
    assert header["followRequestPending"] is True
    assert header["profileVisibility"] == "private"
    for gated in ("followersCount", "followers", "piecesCount", "pieces"):
        assert gated not in header


def test_a_private_collection_lists_nothing_for_others(monkeypatch):
    owner = SimpleNamespace(id=uuid.uuid4(), username="kofi", collection_private=True)
    monkeypatch.setattr(collected, "SessionLocal", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(collected, "find_user_by_username", lambda db, name: owner)
    monkeypatch.setattr(collected.social_dao, "can_view_content", lambda db, user, viewer: True)
    monkeypatch.setattr(collected.block_dao, "is_blocked_either_way", lambda db, a, b: False)
    bought = [SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())]
    monkeypatch.setattr(collected, "collected_pieces", lambda db, buyer: bought)
    monkeypatch.setattr(collected, "batch_list_piece_media", lambda db, ids: {})
    monkeypatch.setattr(collected, "to_item", lambda piece, media, author: {"id": str(piece.id)})

    class _Db:
        def execute(self, *_):
            return SimpleNamespace(scalars=lambda: [])

        def close(self):
            pass

    monkeypatch.setattr(collected, "SessionLocal", _Db)
    app = Flask(__name__)
    with app.test_request_context():
        g.user = {"id": str(uuid.uuid4())}
        assert collected.list_for_user("kofi") == ([], 200)
        g.user = {"id": str(owner.id)}
        items, status = collected.list_for_user("kofi")
        assert status == 200 and len(items) == 1
