"""Scenes and drafts (BACKEND_CHANGES B33), without a database: the payload fields, linked
pieces, deleting a comment, comments off, the draft fields and the Scene share page.

Runs with `pytest tests/unit/test_scenes_b33.py --noconftest`; the same over HTTP against
Postgres is in tests/api/test_scenes_b33.py.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from flask import Flask, g

from src.modules.posts import post_extras, posts_controller
from src.modules.posts.posts_dao import post_to_dict
from src.modules.share import share_routes
from src.modules.social import social_controller
from src.modules.user import drafts
from src.shared.utils.app_error import AppError

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def _post(**overrides):
    values = dict(
        id=uuid.uuid4(), user_id=uuid.uuid4(), media_url="https://cdn.test/a/1.jpg",
        media_type="image", caption="Studio, late night", location=None,
        media_aspect_ratio="9:16", thumbnail_url=None, is_process=False, linked_piece_id=None,
        linked_piece_ids=None, status="live", media_urls=None, comments_off=False,
        draft_step=None, publish_failed=False, created_at=NOW, updated_at=NOW,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _piece(owner, **overrides):
    values = dict(
        id=uuid.uuid4(), user_id=owner, title="Night Studies", media_url="https://cdn.test/p.jpg",
        media_type="image", media_aspect_ratio="3:4", is_for_sale=True, listing_type="fixed",
        price_cents=120_00, currency="USD", status="live", deleted_at=None,
        auction_ends_at=None, auction_starts_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return iter(self._rows)


class FakeDb:
    """Answers each `execute` with the next list given; `get` from a dict."""

    def __init__(self, results=(), rows=None):
        self.results = list(results)
        self.rows = rows or {}
        self.deleted = []
        self.commits = 0

    def execute(self, _statement):
        return _Rows(self.results.pop(0))

    def get(self, _model, key):
        return self.rows.get(key)

    def delete(self, row):
        self.deleted.append(row)

    def add(self, row):
        if getattr(row, "created_at", None) is None:
            row.created_at = NOW  # the column default, which needs a real session
        self.rows[row.id] = row

    def commit(self):
        self.commits += 1

    def refresh(self, _row):
        pass

    def close(self):
        pass


@pytest.fixture()
def app():
    return Flask(__name__)


# --- the payload ----------------------------------------------------------------------------------

def test_a_one_photo_scene_lists_its_photo_and_carries_the_new_fields():
    post = _post(updated_at=NOW + timedelta(hours=1))
    d = post_to_dict(post)

    assert d["mediaUrls"] == ["https://cdn.test/a/1.jpg"]
    assert d["commentsOff"] is False
    assert d["draftStep"] is None and d["publishFailed"] is False
    assert d["updatedAt"] == (NOW + timedelta(hours=1)).isoformat()


def test_several_photos_keep_their_order():
    urls = ["https://cdn.test/a/1.jpg", "https://cdn.test/a/2.jpg", "https://cdn.test/a/3.jpg"]
    assert post_to_dict(_post(media_urls=urls))["mediaUrls"] == urls


@pytest.mark.parametrize(
    "body, media_type, message",
    [
        ({"mediaUrls": "https://cdn.test/a/1.jpg"}, "image", "must be a list"),
        ({"mediaUrls": ["https://cdn.test/a/1.jpg", ""]}, "image", "must be a list"),
        ({"mediaUrls": ["https://cdn.test/v.mp4", "https://cdn.test/w.mp4"]}, "video", "photo Scene"),
        ({"mediaUrls": [f"https://cdn.test/a/{i}.jpg" for i in range(11)]}, "image", "up to 10"),
    ],
)
def test_scene_photos_are_checked(body, media_type, message):
    with pytest.raises(AppError) as error:
        posts_controller._scene_photos(body, media_type)
    assert error.value.status_code == 400
    assert message in error.value.message


def test_no_photo_list_means_the_single_media_url():
    assert posts_controller._scene_photos({"mediaUrl": "x"}, "image") == []
    assert posts_controller._scene_photos({"mediaUrls": ["https://cdn.test/v.mp4"]}, "video") == [
        "https://cdn.test/v.mp4"
    ]


# --- linked pieces --------------------------------------------------------------------------------

def test_linked_pieces_come_in_order_without_deleted_or_others_drafts(monkeypatch):
    monkeypatch.setattr("src.modules.bids.auction_list_fields.add_to", lambda db, pairs: None)
    artist = uuid.uuid4()
    first, second, draft = _piece(artist, title="First"), _piece(artist, title="Second"), _piece(
        artist, title="Unfinished", status="draft"
    )
    gone = uuid.uuid4()
    scene = _post(user_id=artist, linked_piece_id=second.id,
                  linked_piece_ids=[second.id, gone, draft.id, first.id])
    author = SimpleNamespace(id=artist, username="mara", name="Mara Ellis", image=None)

    seen_by_stranger = post_extras.linked_pieces(
        FakeDb([[first, second, draft], [author]]), [scene], uuid.uuid4()
    )
    assert [p["title"] for p in seen_by_stranger[scene.id]] == ["Second", "First"]
    one = seen_by_stranger[scene.id][0]
    assert one["priceCents"] == 120_00 and one["author"]["username"] == "mara"
    assert one["mediaUrl"] == "https://cdn.test/p.jpg"

    seen_by_artist = post_extras.linked_pieces(
        FakeDb([[first, second, draft], [author]]), [scene], artist
    )
    assert [p["title"] for p in seen_by_artist[scene.id]] == ["Second", "Unfinished", "First"]


def test_a_scene_without_links_asks_for_nothing():
    scene = _post()
    assert post_extras.linked_pieces(FakeDb([]), [scene], None) == {scene.id: []}


def test_save_counts_and_links_are_merged(monkeypatch):
    a, b = _post(), _post()
    monkeypatch.setattr(post_extras.social_dao, "batch_save_counts", lambda db, kind, ids: {a.id: 3})
    monkeypatch.setattr(post_extras, "linked_pieces", lambda db, posts, viewer: {a.id: [{"id": "p"}]})
    da, db_ = {}, {}

    post_extras.add_to(None, [(a, da), (b, db_)], None)

    assert da == {"saveCount": 3, "linkedPieces": [{"id": "p"}]}
    assert db_ == {"saveCount": 0, "linkedPieces": []}


# --- comments -------------------------------------------------------------------------------------

def _comment(author, target_id):
    return SimpleNamespace(id=uuid.uuid4(), user_id=author, target_type="post", target_id=target_id)


@pytest.fixture()
def comment_world(monkeypatch):
    artist, writer, stranger = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    scene_id = uuid.uuid4()
    comment = _comment(writer, scene_id)
    fake = FakeDb(rows={comment.id: comment})
    monkeypatch.setattr(social_controller, "SessionLocal", lambda: fake)
    monkeypatch.setattr(social_controller, "_get_target_owner_id", lambda db, kind, tid: artist)
    monkeypatch.setattr(social_controller.social_dao, "count_comments", lambda db, kind, tid: 4)
    return SimpleNamespace(artist=artist, writer=writer, stranger=stranger, comment=comment, db=fake)


@pytest.mark.parametrize("who", ["writer", "artist"])
def test_the_writer_or_the_artist_can_delete_a_comment(app, comment_world, who):
    with app.test_request_context():
        g.user = {"id": str(getattr(comment_world, who))}
        data, status = social_controller.delete_comment(str(comment_world.comment.id))

    assert status == 200
    assert data == {"deleted": True, "commentCount": 4}
    assert comment_world.db.deleted == [comment_world.comment]


def test_anyone_else_is_told_the_comment_isnt_there(app, comment_world):
    with app.test_request_context():
        g.user = {"id": str(comment_world.stranger)}
        with pytest.raises(AppError) as error:
            social_controller.delete_comment(str(comment_world.comment.id))

    assert error.value.status_code == 404
    assert comment_world.db.deleted == []


def test_a_missing_or_malformed_comment_is_not_found(app, comment_world):
    with app.test_request_context():
        g.user = {"id": str(comment_world.writer)}
        for raw in (str(uuid.uuid4()), "not-an-id"):
            with pytest.raises(AppError) as error:
                social_controller.delete_comment(raw)
            assert error.value.status_code == 404


def _commenting(monkeypatch, app, scene):
    me = SimpleNamespace(id=uuid.uuid4(), username="jo", name="Jo")
    fake = FakeDb(rows={scene.id: scene})
    monkeypatch.setattr(social_controller, "SessionLocal", lambda: fake)
    monkeypatch.setattr(social_controller, "get_user_by_id", lambda db, uid: me)
    monkeypatch.setattr(social_controller.notifications_dao, "create_and_push", lambda *a, **k: None)
    monkeypatch.setattr(social_controller.social_dao, "comment_to_dict", lambda c, u: {"id": str(c.id)})
    return fake, me


def test_comments_off_refuses_a_new_comment(monkeypatch, app):
    scene = _post(comments_off=True, deleted_at=None)
    fake, me = _commenting(monkeypatch, app, scene)

    with app.test_request_context(json={"body": "Lovely"}):
        g.user = {"id": str(me.id)}
        with pytest.raises(AppError) as error:
            social_controller.add_comment("post", str(scene.id))

    assert error.value.status_code == 403
    assert error.value.message == "Comments are off for this Scene."
    assert fake.commits == 0


def test_comments_on_still_take_one(monkeypatch, app):
    scene = _post(deleted_at=None)
    fake, me = _commenting(monkeypatch, app, scene)

    with app.test_request_context(json={"body": "Lovely"}):
        g.user = {"id": str(me.id)}
        data, status = social_controller.add_comment("post", str(scene.id))

    assert status == 201 and data["body"] == "Lovely"


def test_a_comment_on_nothing_is_not_found(monkeypatch, app):
    fake, me = _commenting(monkeypatch, app, _post(deleted_at=None))

    with app.test_request_context(json={"body": "Hello"}):
        g.user = {"id": str(me.id)}
        with pytest.raises(AppError) as error:
            social_controller.add_comment("post", str(uuid.uuid4()))

    assert error.value.status_code == 404


# --- drafts ---------------------------------------------------------------------------------------

def test_draft_fields_are_checked_and_set():
    row = SimpleNamespace(draft_step=None, publish_failed=False, status="draft")
    drafts.apply_fields(row, {"draftStep": "details", "publishFailed": True})
    assert (row.draft_step, row.publish_failed) == ("details", True)

    for body in ({"draftStep": "Price & shipping"}, {"draftStep": 3}, {"publishFailed": "yes"}):
        with pytest.raises(AppError) as error:
            drafts.apply_fields(row, body)
        assert error.value.status_code == 400

    drafts.apply_fields(row, {"draftStep": None})
    assert row.draft_step is None


def test_going_live_clears_the_draft_fields():
    row = SimpleNamespace(draft_step="price", publish_failed=True, status="draft")
    drafts.clear_when_live(row)
    assert (row.draft_step, row.publish_failed) == ("price", True), "still a draft"

    row.status = "live"
    drafts.clear_when_live(row)
    assert (row.draft_step, row.publish_failed) == (None, False)


def test_drafts_list_both_kinds_newest_edit_first(monkeypatch):
    me = uuid.uuid4()
    old_piece = SimpleNamespace(id=uuid.uuid4(), updated_at=NOW - timedelta(days=2), created_at=NOW)
    new_piece = SimpleNamespace(id=uuid.uuid4(), updated_at=NOW, created_at=NOW)
    scene = _post(user_id=me, status="draft", updated_at=NOW - timedelta(days=1), publish_failed=True)
    fake = FakeDb([[old_piece, new_piece], [scene]])
    monkeypatch.setattr(drafts, "SessionLocal", lambda: fake)
    monkeypatch.setattr("src.modules.pieces.pieces_dao.batch_list_piece_media", lambda db, ids: {})
    monkeypatch.setattr(
        "src.modules.pieces.pieces_dao.piece_to_dict", lambda p, media=None: {"id": str(p.id)}
    )

    data, status = drafts.list_for_me(me)

    assert status == 200
    assert [(d["type"], d["id"]) for d in data["items"]] == [
        ("piece", str(new_piece.id)),
        ("post", str(scene.id)),
        ("piece", str(old_piece.id)),
    ]
    assert data["items"][1]["publishFailed"] is True


# --- the share page -------------------------------------------------------------------------------

@pytest.fixture()
def share_client(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(share_routes.share_bp, url_prefix="/share")
    posts = {}
    monkeypatch.setattr(share_routes, "SessionLocal", lambda: FakeDb())
    monkeypatch.setattr("src.modules.posts.posts_dao.get_post", lambda db, pid: posts.get(pid))
    monkeypatch.setattr(
        share_routes, "get_user_by_id", lambda db, uid: SimpleNamespace(name="Mara Ellis")
    )
    return app.test_client(), posts


def test_a_scene_link_previews_with_the_artist_and_caption(share_client):
    client, posts = share_client
    scene = _post()
    posts[scene.id] = scene

    page = client.get(f"/share/post/{scene.id}", headers={"User-Agent": "WhatsApp/2.0"})

    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "A Scene by Mara Ellis" in html
    assert "Studio, late night" in html
    assert "https://cdn.test/a/1.jpg" in html


def test_a_draft_or_missing_scene_has_no_page(share_client):
    client, posts = share_client
    draft = _post(status="draft")
    posts[draft.id] = draft

    for pid in (draft.id, uuid.uuid4()):
        page = client.get(f"/share/post/{pid}", headers={"User-Agent": "WhatsApp/2.0"})
        assert page.status_code == 404
