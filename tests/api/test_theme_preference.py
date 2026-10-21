"""Appearance preference on the signed-in user.

Null means the account has never chosen (the client treats that as light).
PATCH /api/user/me accepts only "light" or "dark".
"""
from tests.factories import make_user


def _data(response) -> dict:
    return response.get_json()["data"]


def test_theme_preference_starts_unset_then_round_trips(db, client, auth_headers):
    user = make_user(db)
    headers = auth_headers(user)

    before = client.get("/api/user/me", headers=headers)
    assert before.status_code == 200
    assert _data(before)["themePreference"] is None

    dark = client.patch("/api/user/me", json={"themePreference": "dark"}, headers=headers)
    assert dark.status_code == 200
    assert _data(dark)["themePreference"] == "dark"

    again = client.get("/api/user/me", headers=headers)
    assert again.status_code == 200
    assert _data(again)["themePreference"] == "dark"

    light = client.patch("/api/user/me", json={"themePreference": "light"}, headers=headers)
    assert light.status_code == 200
    assert _data(light)["themePreference"] == "light"


def test_theme_preference_rejects_anything_else_and_keeps_the_saved_value(db, client, auth_headers):
    user = make_user(db)
    headers = auth_headers(user)
    saved = client.patch("/api/user/me", json={"themePreference": "dark"}, headers=headers)
    assert saved.status_code == 200

    for bad in ("system", "", None, "LIGHT"):
        response = client.patch(
            "/api/user/me", json={"themePreference": bad}, headers=headers
        )
        assert response.status_code == 400, bad
        assert response.get_json()["success"] is False

    still = client.get("/api/user/me", headers=headers)
    assert _data(still)["themePreference"] == "dark"


def test_theme_preference_is_private(db, client, auth_headers):
    owner = make_user(db)
    client.patch(
        "/api/user/me", json={"themePreference": "dark"}, headers=auth_headers(owner)
    )
    viewer = make_user(db)

    public = client.get(f"/api/user/{owner.username}", headers=auth_headers(viewer))
    assert public.status_code == 200
    assert "themePreference" not in _data(public)
