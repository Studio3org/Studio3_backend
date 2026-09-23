"""Deleting your own account — anonymized, not hard-deleted, and asked why on the way out.

The "why" is the one thing worth keeping: it's written to the audit log with the account's
*real* name/username still in the label, before the row itself gets scrubbed, so an admin can
still answer "who was this and why did they leave" from the console — see
auth_controller.delete_account's own docstring for the ordering that makes that true.
"""
from src.modules.auth.auth_controller import DELETION_REASONS, _hash_password
from src.shared.models.audit import AuditEvent
from tests.factories import make_user

PASSWORD = "correct horse battery staple"


def _user_with_password(db, **overrides):
    return make_user(db, password=_hash_password(PASSWORD), **overrides)


def test_a_reason_is_optional(db, client, auth_headers):
    user = _user_with_password(db)

    response = client.delete("/api/users/me", json={"password": PASSWORD},
                             headers=auth_headers(user))

    assert response.status_code == 200
    db.refresh(user)
    assert user.deleted_at is not None


def test_an_unrecognized_reason_is_rejected(db, client, auth_headers):
    user = _user_with_password(db)

    response = client.delete(
        "/api/users/me",
        json={"password": PASSWORD, "reason": "just because"},
        headers=auth_headers(user),
    )

    assert response.status_code == 400


def test_the_wrong_password_is_still_rejected_even_with_a_reason(db, client, auth_headers):
    user = _user_with_password(db)

    response = client.delete(
        "/api/users/me",
        json={"password": "not it", "reason": "not_using"},
        headers=auth_headers(user),
    )

    assert response.status_code == 401
    db.refresh(user)
    assert user.deleted_at is None


def test_every_defined_reason_is_accepted(db, client, auth_headers):
    for reason in DELETION_REASONS:
        user = _user_with_password(db)

        response = client.delete(
            "/api/users/me",
            json={"password": PASSWORD, "reason": reason},
            headers=auth_headers(user),
        )

        assert response.status_code == 200, (reason, response.get_json())


def test_deleting_anonymizes_the_account_and_records_why(db, client, auth_headers):
    user = _user_with_password(db, name="Priya Patel", username="priyapatel")
    original_username = user.username
    user_id = user.id

    response = client.delete(
        "/api/users/me",
        json={
            "password": PASSWORD,
            "reason": "fees_too_high",
            "feedback": "The commission ate too much of what I made on a sale.",
        },
        headers=auth_headers(user),
    )

    assert response.status_code == 200
    assert response.get_json()["data"]["deleted"] is True

    db.expire_all()
    row = db.query(AuditEvent).filter_by(subject_id=user_id, action="account_deleted").one()
    # The label has to survive the anonymization it was captured just ahead of — this is
    # the whole point of doing it before the fields below are overwritten, not after.
    assert original_username in row.actor_label
    assert "Priya Patel" in row.actor_label
    assert row.detail["reason"] == "fees_too_high"
    assert "commission" in row.detail["feedback"]
    assert "fees too high" in row.note

    from src.shared.models.user import User

    anonymized = db.get(User, user_id)
    assert anonymized.username != original_username
    assert anonymized.name == "Deleted user"
    assert anonymized.deleted_at is not None


def test_feedback_is_optional(db, client, auth_headers):
    user = _user_with_password(db)

    response = client.delete(
        "/api/users/me",
        json={"password": PASSWORD, "reason": "not_using"},
        headers=auth_headers(user),
    )

    assert response.status_code == 200
    db.expire_all()
    row = db.query(AuditEvent).filter_by(subject_id=user.id, action="account_deleted").one()
    assert row.detail["feedback"] is None


def test_the_reason_is_visible_in_the_admin_audit_log(db, client, auth_headers):
    admin = make_user(db, is_admin=True)
    leaving = _user_with_password(db, name="Sam Okafor", username="samokafor")

    client.delete(
        "/api/users/me",
        json={"password": PASSWORD, "reason": "privacy_concerns"},
        headers=auth_headers(leaving),
    )

    body = client.get(
        "/api/admin/audit?action=account_deleted", headers=auth_headers(admin)
    ).get_json()["data"]

    assert body["entries"], "the deletion just made should show up under its own filter"
    entry = body["entries"][0]
    assert "samokafor" in entry["actorLabel"]
    assert entry["detail"]["reason"] == "privacy_concerns"
