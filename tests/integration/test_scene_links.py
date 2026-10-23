"""A Scene can show on several pieces (posts.linked_piece_ids)."""
import uuid

import pytest

from src.modules.posts import posts_controller
from src.modules.posts.posts_dao import list_related_posts, post_to_dict
from src.shared.models.post import Post
from src.shared.utils.app_error import AppError
from tests.factories import make_piece, make_user


def _scene(db, owner, **links):
    post = Post(id=uuid.uuid4(), user_id=owner.id, media_url="https://example.test/s.jpg",
                media_type="image", **links)
    db.add(post)
    db.commit()
    return post


def test_a_scene_shows_on_every_linked_piece(db):
    artist = make_user(db, seller=True)
    first, second = make_piece(db, artist), make_piece(db, artist)
    scene = _scene(db, artist, linked_piece_id=first.id, linked_piece_ids=[first.id, second.id])

    assert [p.id for p in list_related_posts(db, first.id)] == [scene.id]
    assert [p.id for p in list_related_posts(db, second.id)] == [scene.id]
    assert post_to_dict(scene)["linkedPieceIds"] == [str(first.id), str(second.id)]


def test_older_single_links_still_read_as_a_list(db):
    artist = make_user(db, seller=True)
    piece = make_piece(db, artist)
    scene = _scene(db, artist, linked_piece_id=piece.id)

    assert post_to_dict(scene)["linkedPieceIds"] == [str(piece.id)]
    assert [p.id for p in list_related_posts(db, piece.id)] == [scene.id]


def test_only_your_own_pieces_can_be_linked(db):
    artist, other = make_user(db, seller=True), make_user(db, seller=True)
    mine, theirs = make_piece(db, artist), make_piece(db, other)

    ids = posts_controller._resolve_linked_pieces(
        db, artist, {"linkedPieceIds": [str(mine.id), str(mine.id)]}
    )
    assert ids == [mine.id]
    with pytest.raises(AppError):
        posts_controller._resolve_linked_pieces(db, artist, {"linkedPieceIds": [str(theirs.id)]})
