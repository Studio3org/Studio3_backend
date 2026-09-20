"""App association, and the codes that go on the wall.

These two files are what make a scanned QR or a shared link open the app instead of a
browser. They fail silently when wrong, so the things worth asserting are the ones with no
error message: the path, the content type, and that a placeholder never ships.
"""
from src.shared.config import app_links
from tests.factories import make_event, make_piece, make_user


# --- where they are served ------------------------------------------------------------------

def test_the_files_are_at_the_site_root(client):
    """Apple and Google fetch a fixed, unprefixed path. Served under /share they would be at
    /share/.well-known/..., which nothing requests — and association would fail with nothing
    anywhere to say why."""
    assert client.get("/.well-known/apple-app-site-association").status_code == 200
    assert client.get("/.well-known/assetlinks.json").status_code == 200


def test_they_are_json_and_need_no_auth(client):
    """Both platforms fetch these unauthenticated, and a redirect counts as a failure."""
    for path in ("/.well-known/apple-app-site-association", "/.well-known/assetlinks.json"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.mimetype == "application/json", path


# --- what they contain ------------------------------------------------------------------------

def test_an_unconfigured_deployment_associates_nothing_rather_than_a_placeholder(
    client, monkeypatch
):
    """A file containing REPLACE_WITH_TEAM_ID looks configured and fails in a way that takes
    an afternoon to work out. An empty one plainly associates nothing."""
    monkeypatch.delenv("IOS_TEAM_ID", raising=False)
    monkeypatch.delenv("ANDROID_CERT_FINGERPRINTS", raising=False)

    aasa = client.get("/.well-known/apple-app-site-association").get_json()
    links = client.get("/.well-known/assetlinks.json").get_json()

    assert aasa == {"applinks": {"details": []}}
    assert links == []
    assert "REPLACE" not in str(aasa) + str(links)


def test_a_configured_deployment_names_the_app(client, monkeypatch):
    monkeypatch.setenv("IOS_TEAM_ID", "5XVXDNBNKN")
    monkeypatch.setenv("IOS_BUNDLE_ID", "com.studio3.discover")

    body = client.get("/.well-known/apple-app-site-association").get_json()

    detail = body["applinks"]["details"][0]
    assert detail["appIDs"] == ["5XVXDNBNKN.com.studio3.discover"]


def test_every_link_shape_the_app_produces_is_covered(client, monkeypatch):
    """The app's own share links point at /share/... — if the AASA only matched /piece/*, a
    link the app generated would open the app and then do nothing."""
    monkeypatch.setenv("IOS_TEAM_ID", "5XVXDNBNKN")

    body = client.get("/.well-known/apple-app-site-association").get_json()
    paths = {c["/"] for c in body["applinks"]["details"][0]["components"]}

    assert {"/piece/*", "/series/*", "/event/*"} <= paths
    assert {"/share/piece/*", "/share/series/*", "/share/event/*"} <= paths


def test_several_signing_keys_are_all_accepted(client, monkeypatch):
    """A project legitimately has more than one: the release key, the debug key that
    internally-shared builds are signed with, and Play's own re-signing key later. Listing
    one means links verify for some of your builds and silently not others."""
    monkeypatch.setenv("ANDROID_CERT_FINGERPRINTS", "AA:BB, cc:dd ,")

    body = client.get("/.well-known/assetlinks.json").get_json()

    assert body[0]["target"]["sha256_cert_fingerprints"] == ["AA:BB", "CC:DD"]
    assert body[0]["target"]["package_name"] == "com.studio3.discover"
    assert body[0]["relation"] == ["delegate_permission/common.handle_all_urls"]


def test_is_configured_reports_whether_association_can_work(monkeypatch):
    monkeypatch.delenv("IOS_TEAM_ID", raising=False)
    monkeypatch.delenv("ANDROID_CERT_FINGERPRINTS", raising=False)
    assert app_links.is_configured() is False

    monkeypatch.setenv("IOS_TEAM_ID", "5XVXDNBNKN")
    assert app_links.is_configured() is True


# --- the share page a scan lands on ----------------------------------------------------------

def test_a_published_event_has_a_share_page(db, client):
    host = make_user(db, seller=True)
    event = make_event(db, host, status="published", title="Collector's Preview")

    response = client.get(f"/share/event/{event.id}")

    assert response.status_code == 200
    assert b"Collector's Preview" in response.data or b"Collector&#39;s Preview" in response.data


def test_a_draft_event_is_not_rendered_by_its_share_page(db, client):
    """A link is the one way somebody who is not the host could otherwise see a draft.

    Asserts the property rather than a status code: the share module's not-found path
    redirects to the web app rather than returning 404, which is its established behaviour
    for pieces and series too. What matters is that the draft's details never reach the
    page.
    """
    host = make_user(db, seller=True)
    event = make_event(db, host, status="draft", title="Not announced yet")

    response = client.get(f"/share/event/{event.id}")

    assert response.status_code != 200
    assert b"Not announced yet" not in response.data


# --- the codes for the room -------------------------------------------------------------------

def test_the_host_gets_a_link_per_piece_on_the_bill(db, client, auth_headers):
    host = make_user(db, seller=True)
    piece = make_piece(db, host, price_cents=300_00, status="draft")
    event = make_event(db, host, status="published")
    client.post(
        f"/api/events/{event.id}/pieces",
        json={"pieceId": str(piece.id), "mode": "bid", "priceCents": 400_00},
        headers=auth_headers(host),
    )

    response = client.get(f"/api/events/{event.id}/qr-codes", headers=auth_headers(host))

    assert response.status_code == 200
    body = response.get_json()["data"]
    assert len(body["pieces"]) == 1
    entry = body["pieces"][0]
    assert entry["pieceId"] == str(piece.id)
    # A link, not a token: scanning it navigates, it does not admit anyone.
    assert entry["url"].endswith(f"/share/piece/{piece.id}")
    assert body["eventUrl"].endswith(f"/share/event/{event.id}")


def test_the_codes_are_not_public(db, client, auth_headers):
    host, stranger = make_user(db, seller=True), make_user(db)
    event = make_event(db, host, status="published")

    response = client.get(f"/api/events/{event.id}/qr-codes", headers=auth_headers(stranger))

    assert response.status_code == 404
