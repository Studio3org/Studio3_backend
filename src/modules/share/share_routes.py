"""Public, unauthenticated share pages for pieces and series.

These exist purely so a link pasted into WhatsApp/iMessage/Slack/Twitter/Facebook gets a
rich preview (og:title/og:image/og:description) — something the client-rendered web app
(`app/`) can't produce on its own without full SSR. A crawler fetching this URL gets a tiny
static HTML page with the right meta tags and stops there; an actual person is redirected
straight on to the real web app. Mounted outside `/api` (HTML, not JSON), like `/admin`.
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


@share_bp.get("/piece/<piece_id>")
def share_piece(piece_id):
    web_url = f"{_web_base_url()}/piece/{piece_id}"
    db = SessionLocal()
    try:
        piece = get_piece(db, _uuid_or_none(piece_id))
        if not piece:
            return _not_found(web_url)
        if not _is_crawler():
            return redirect(web_url, code=302)
        author = get_user_by_id(db, piece.user_id)
        return render_template(
            "share/preview.html",
            title=f"{piece.title} by {author.name if author else 'an artist'}",
            description=(piece.caption or "").strip() or "See this piece on Studio 3.",
            image_url=piece.media_url,
            canonical_url=web_url,
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
        if not _is_crawler():
            return redirect(web_url, code=302)
        summary = series_dao.series_summary_dict(db, series)
        author = get_user_by_id(db, series.user_id)
        title = f"{series.name} by {author.name}" if author else series.name
        return render_template(
            "share/preview.html",
            title=title,
            description=(series.description or "").strip()
            or f"A series of {summary['pieceCount']} pieces on Studio 3.",
            image_url=summary.get("coverUrl"),
            canonical_url=web_url,
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
