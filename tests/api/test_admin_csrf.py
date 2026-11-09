"""The admin console's forms are cookie-authenticated, so their writes must carry a CSRF token.

The shared `app` fixture switches CSRF off so form tests can post directly; this builds an app
with it left on, the way every deployed environment runs, to prove the check still bites.
"""
from datetime import datetime, timezone

import pytest

from src.modules.admin.admin_auth import SESSION_STARTED_KEY, SESSION_USER_KEY
from tests.factories import make_event, make_user


@pytest.fixture
def csrf_client(migrated_database):
    from src.app import create_app

    return create_app({"TESTING": True}).test_client()


def test_admin_form_post_without_a_csrf_token_is_refused(db, csrf_client):
    admin = make_user(db, is_admin=True)
    event = make_event(db, make_user(db, seller=True))
    with csrf_client.session_transaction() as session:
        session[SESSION_USER_KEY] = str(admin.id)
        session[SESSION_STARTED_KEY] = datetime.now(timezone.utc).timestamp()

    response = csrf_client.post(f"/admin/events/{event.id}/feature", data={"featured": "1"})

    assert response.status_code == 400
