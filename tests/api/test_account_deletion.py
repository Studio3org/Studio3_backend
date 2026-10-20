"""Deleting your own account — the User row is anonymized (see the deleted_at column
comment on the model for why it's not a hard DELETE), but everything that belongs to this
user alone — pieces, scenes, hosted events — is actually removed, the same as the ⋯ menu's
own delete action on each of those. See auth_controller.delete_account's own docstring.
"""
from src.modules.auth.auth_controller import _hash_password
from src.shared.models.audit import AuditEvent
from src.shared.models.event import Event
from src.shared.models.piece import Piece
from src.shared.models.post import Post
from tests.factories import make_event, make_piece, make_user

PASSWORD = "correct horse battery staple"


def _user_with_password(db, **overrides):
    return make_user(db, password=_hash_password(PASSWORD), **overrides)


def _make_post(db, owner, **overrides):
    import uuid

    overrides.setdefault("media_url", "https://example.test/post.jpg")
    overrides.setdefault("media_type", "image")
    post = Post(id=uuid.uuid4(), user_id=owner.id, **overrides)
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


def test_deletes_the_account(db, client, auth_headers):
    user = _user_with_password(db)

    response = client.delete("/api/user/me", json={"password": PASSWORD},
                             headers=auth_headers(user))

    assert response.status_code == 200
    db.refresh(user)
    assert user.deleted_at is not None
    assert user.name == "Deleted user"


def test_the_wrong_password_is_rejected(db, client, auth_headers):
    user = _user_with_password(db)

    response = client.delete(
        "/api/user/me", json={"password": "not it"}, headers=auth_headers(user)
    )

    assert response.status_code == 401
    db.refresh(user)
    assert user.deleted_at is None


def test_deleting_anonymizes_the_account_and_records_it(db, client, auth_headers):
    user = _user_with_password(db, name="Priya Patel", username="priyapatel")
    original_username = user.username
    user_id = user.id

    response = client.delete(
        "/api/user/me", json={"password": PASSWORD}, headers=auth_headers(user)
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["deleted"] is True

    db.expire_all()
    row = db.query(AuditEvent).filter_by(subject_id=user_id, action="account_deleted").one()
    # The label has to survive the anonymization it was captured just ahead of — this is
    # the whole point of doing it before the fields below are overwritten, not after.
    assert original_username in row.actor_label
    assert "Priya Patel" in row.actor_label

    from src.shared.models.user import User

    anonymized = db.get(User, user_id)
    assert anonymized.username != original_username
    assert anonymized.name == "Deleted user"
    assert anonymized.deleted_at is not None


def test_the_deletion_is_visible_in_the_admin_audit_log(db, client, auth_headers):
    admin = make_user(db, is_admin=True)
    leaving = _user_with_password(db, name="Sam Okafor", username="samokafor")

    client.delete("/api/user/me", json={"password": PASSWORD}, headers=auth_headers(leaving))

    body = client.get(
        "/api/admin/audit?action=account_deleted", headers=auth_headers(admin)
    ).get_json()["data"]

    assert body["entries"], "the deletion just made should show up under its own filter"
    entry = body["entries"][0]
    assert "samokafor" in entry["actorLabel"]


def test_a_live_piece_is_deleted_not_left_anonymized(db, client, auth_headers):
    user = _user_with_password(db)
    piece = make_piece(db, user, status="live", is_for_sale=False)

    response = client.delete("/api/user/me", json={"password": PASSWORD},
                             headers=auth_headers(user))

    assert response.status_code == 200
    db.expire_all()
    stored = db.get(Piece, piece.id)
    assert stored.status == "deleted"
    assert stored.deleted_at is not None


def test_a_sold_piece_is_left_alone(db, client, auth_headers):
    """It's the buyer's order history now, not just this seller's listing."""
    user = _user_with_password(db)
    piece = make_piece(db, user, status="sold", is_for_sale=False)

    response = client.delete("/api/user/me", json={"password": PASSWORD},
                             headers=auth_headers(user))

    assert response.status_code == 200
    db.expire_all()
    stored = db.get(Piece, piece.id)
    assert stored.status == "sold"
    assert stored.deleted_at is None


def test_a_scene_is_deleted(db, client, auth_headers):
    user = _user_with_password(db)
    post = _make_post(db, user)

    response = client.delete("/api/user/me", json={"password": PASSWORD},
                             headers=auth_headers(user))

    assert response.status_code == 200
    db.expire_all()
    stored = db.get(Post, post.id)
    assert stored.deleted_at is not None
    assert stored.status == "deleted"


def test_a_draft_event_is_removed_outright(db, client, auth_headers):
    user = _user_with_password(db)
    event = make_event(db, user, status="draft")
    event_id = event.id

    response = client.delete("/api/user/me", json={"password": PASSWORD},
                             headers=auth_headers(user))

    assert response.status_code == 200
    db.expire_all()
    assert db.get(Event, event_id) is None


def test_a_published_event_is_cancelled_and_removed(db, client, auth_headers):
    user = _user_with_password(db)
    event = make_event(db, user, status="published")
    event_id = event.id

    response = client.delete("/api/user/me", json={"password": PASSWORD},
                             headers=auth_headers(user))

    assert response.status_code == 200
    db.expire_all()
    assert db.get(Event, event_id) is None
