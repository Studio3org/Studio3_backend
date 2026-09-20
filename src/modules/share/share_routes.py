"""Public, unauthenticated share pages for pieces and series.

Three audiences hit these URLs, and each gets something different:
  - A link-preview crawler (WhatsApp/iMessage/Slack/Twitter/Facebook/etc., detected by
    User-Agent) gets a tiny static page with real og:title/og:image/og:description — the
    client-rendered web app (`app/`) can't produce that on its own without full SSR.
  - A real visitor gets an interstitial that tries `studio3://...` (a custom URL scheme,
    which — unlike Universal Links/App Links — needs no domain verification, so it can open
    the installed app today even before that's set up) and falls back to App Store/Play
    Store badges if the app doesn't open within ~1.2s, with a "continue in browser" escape
    hatch to the real web app.
  - Not-found just redirects to the web app's home.
Mounted outside `/api` (HTML, not JSON), like `/admin`.
"""
import os

from flask import Blueprint, redirect, render_template, request

from src.shared.config.database import SessionLocal
from src.modules.pieces.pieces_dao import get_piece
from src.modules.series import series_dao
from src.modules.user.user_dao import get_user_by_id

share_bp = Blueprint("share", __name__, template_folder="../../templates")

# Substrings found in the User-Agent of link-preview crawlers for the major share targets.
# Not exhaustive (iMessage's fetcher in particular doesn't consistently self-identify), but
# covers the common cases — see module docstring.
_CRAWLER_UA_MARKERS = (
    "facebookexternalhit",
    "twitterbot",
    "whatsapp",
    "slackbot",
    "telegrambot",
    "discordbot",
    "linkedinbot",
    "skypeuripreview",
    "pinterest",
    "google-inspectiontool",
    "applebot",
)


def _is_crawler() -> bool:
    ua = (request.headers.get("User-Agent") or "").lower()
    return any(marker in ua for marker in _CRAWLER_UA_MARKERS)


def _web_base_url() -> str:
    return (os.getenv("FRONTEND_URL") or "https://studio-3.co").rstrip("/")


# PLACEHOLDER: these apps aren't published yet. Set APP_STORE_URL/PLAY_STORE_URL once they
# are — until then the "get the app" banner's badges are dead links, same spirit as the
# other REPLACE_WITH_* placeholders in app/public/.well-known/.
def _app_store_url() -> str:
    return os.getenv("APP_STORE_URL") or "https://apps.apple.com/app/id0000000000"


def _play_store_url() -> str:
    return os.getenv("PLAY_STORE_URL") or "https://play.google.com/store/apps/details?id=com.example.studio3"


def _open_app_page(*, deep_link: str, web_url: str, title: str, description: str, image_url):
    if _is_crawler():
        return render_template(
            "share/preview.html",
            title=title,
            description=description,
            image_url=image_url,
            canonical_url=web_url,
        )
    return render_template(
        "share/open_app.html",
        title=title,
        description=description,
        image_url=image_url,
        deep_link=deep_link,
        canonical_url=web_url,
        app_store_url=_app_store_url(),
        play_store_url=_play_store_url(),
    )


@share_bp.get("/piece/<piece_id>")
def share_piece(piece_id):
    web_url = f"{_web_base_url()}/piece/{piece_id}"
    db = SessionLocal()
    try:
        piece = get_piece(db, _uuid_or_none(piece_id))
        if not piece:
            return _not_found(web_url)
        author = get_user_by_id(db, piece.user_id)
        return _open_app_page(
            deep_link=f"studio3://piece/{piece_id}",
            web_url=web_url,
            title=f"{piece.title} by {author.name if author else 'an artist'}",
            description=(piece.caption or "").strip() or "See this piece on Studio 3.",
            image_url=piece.media_url,
        )
    finally:
        db.close()


@share_bp.get("/series/<series_id>")
def share_series(series_id):
    web_url = f"{_web_base_url()}/series/{series_id}"
    db = SessionLocal()
    try:
        series = series_dao.get_series(db, _uuid_or_none(series_id))
        if not series:
            return _not_found(web_url)
        summary = series_dao.series_summary_dict(db, series)
        author = get_user_by_id(db, series.user_id)
        title = f"{series.name} by {author.name}" if author else series.name
        return _open_app_page(
            deep_link=f"studio3://series/{series_id}",
            web_url=web_url,
            title=title,
            description=(series.description or "").strip()
            or f"A series of {summary['pieceCount']} pieces on Studio 3.",
            image_url=summary.get("coverUrl"),
        )
    finally:
        db.close()


@share_bp.get("/event/<event_id>")
def share_event(event_id):
    """The page a scanned QR code or a shared event link lands on.

    Only a published event is shareable. A draft answers not-found rather than rendering,
    because a link is the one way somebody who is not the host could otherwise see one.
    """
    from src.modules.events import events_dao
    from src.shared.models.event import EVENT_PUBLISHED

    web_url = f"{_web_base_url()}/event/{event_id}"
    db = SessionLocal()
    try:
        event = events_dao.get_event(db, _uuid_or_none(event_id))
        if not event or event.status != EVENT_PUBLISHED:
            return _not_found(web_url)
        host = get_user_by_id(db, event.host_id)
        where = event.venue_name or event.address or ""
        return _open_app_page(
            deep_link=f"studio3://event/{event_id}",
            web_url=web_url,
            title=event.title,
            description=(event.description or "").strip()
            or (f"Hosted by {host.name} at {where}." if host and where else "An event on Studio 3."),
            image_url=event.cover_media_url,
        )
    finally:
        db.close()


def _uuid_or_none(raw: str):
    import uuid

    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError, AttributeError):
        return None


def _not_found(fallback_url: str):
    if not _is_crawler():
        return redirect(_web_base_url(), code=302)
    return render_template(
        "share/preview.html",
        title="Studio 3",
        description="This piece is no longer available.",
        image_url=None,
        canonical_url=fallback_url,
    ), 404
