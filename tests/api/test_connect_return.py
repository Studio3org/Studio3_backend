"""Where Stripe sends an artist after Connect onboarding.

These pages did not exist. The whole flow had never run to completion — first a malformed
API key, then Accounts v1 being refused, then no HTTPS — so the first artist to finish
payout setup reached the end and got a 404 from their own backend.
"""
from src.shared.config import app_links


def test_stripe_can_return_an_artist_without_a_404(client):
    """The regression. Stripe redirects to <base>/return, and every Connect route lived
    under /api/artists, so nothing served the path Stripe was given."""
    assert client.get("/connect/return").status_code == 200


def test_an_expired_link_lands_somewhere_too(client):
    """Account Links are single-use and last minutes, so this is reached by going back or
    leaving the tab open — not an edge case."""
    assert client.get("/connect/refresh").status_code == 200


def test_both_pages_offer_a_way_back_into_the_app(client):
    """The artist came from the app and wants to be back in it. The custom scheme is used
    rather than the https link because it needs no domain verification."""
    for path, scheme in (("/connect/return", "studio3://connect/return"),
                         ("/connect/refresh", "studio3://connect/refresh")):
        body = client.get(path).get_data(as_text=True)
        assert scheme in body, path


def test_the_pages_need_no_session(client):
    """Stripe redirects a bare browser with no cookie and no token. Anything gated would
    401 the artist at the last step of onboarding."""
    assert client.get("/connect/return", headers={}).status_code == 200


def test_the_pages_claim_nothing_about_the_account(client):
    """Stripe sends everyone here whether or not they finished, so the page cannot promise
    the artist is payable. That is decided by the account.updated webhook and read back
    through /api/artists/connect/status."""
    body = client.get("/connect/return").get_data(as_text=True).lower()
    for claim in ("you can now", "approved", "verified", "enabled", "ready to receive"):
        assert claim not in body, f"page asserts {claim!r}, which it cannot know"


def test_the_paths_are_claimed_for_deep_linking(client):
    """The other half. Without these in LINK_PATHS the platform never hands the URL to the
    app, so the artist finishes onboarding in a browser instead of back where they started.
    """
    claimed = {path for path, _ in app_links.LINK_PATHS}
    assert "/connect/return" in claimed
    assert "/connect/refresh" in claimed


def test_the_association_file_advertises_them(client, monkeypatch):
    monkeypatch.setenv("IOS_TEAM_ID", "ABCDE12345")
    body = client.get("/.well-known/apple-app-site-association").get_json()
    components = body["applinks"]["details"][0]["components"]
    paths = {c["/"] for c in components}
    assert {"/connect/return", "/connect/refresh"} <= paths


def test_the_pages_are_not_indexed(client):
    """A landing page for one person mid-flow has no business in search results."""
    assert "noindex" in client.get("/connect/return").get_data(as_text=True)
