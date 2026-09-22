"""Events controller — the host's create flow, and the public event browse and detail."""
import uuid
from datetime import datetime, timezone
from typing import Optional

from flask import g, request
from sqlalchemy import func

from src.shared.config.database import SessionLocal
from src.shared.models.event import (
    EVENT_CANCELLED,
    EVENT_PUBLISHED,
    PIECE_FEATURED,
    ROLE_ARTIST,
    ROLE_COHOST,
    Event,
    EventPiece,
)
from src.shared.models.piece import Piece
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.admin import audit_service
from src.shared.models import audit
from src.modules.events import event_state, events_dao, lineup_service, rsvp_service
from src.modules.pieces.pieces_dao import get_piece
from src.modules.user.user_dao import get_user_by_id

logger = get_logger(__name__)


def _viewer_id() -> Optional[uuid.UUID]:
    user = getattr(g, "user", None)
    return uuid.UUID(user["id"]) if user else None


def _require_host(db, event_id: str) -> tuple[Event, uuid.UUID]:
    """Load an event the caller is allowed to edit.

    404 rather than 403 for someone else's event: whether a given id is a real draft is not
    information a stranger should be able to probe for.
    """
    viewer_id = _viewer_id()
    event = events_dao.get_event(db, _uuid(event_id, "event"))
    if not event or event.host_id != viewer_id:
        raise AppError("Event not found.", 404)
    return event, viewer_id


def _uuid(raw: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        raise AppError(f"Invalid {what} id.", 400) from None


def _parse_time(raw, field: str) -> datetime:
    if not raw:
        raise AppError(f"{field} is required.", 400)
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        raise AppError(f"{field} must be an ISO 8601 timestamp.", 400) from None
    # Naive input is read as UTC rather than rejected: the client sends UTC, and the event's
    # own local time is carried separately in `timezone` for display.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- host: create and edit ------------------------------------------------------------------

def create():
    body = request.get_json() or {}
    db = SessionLocal()
    try:
        host = get_user_by_id(db, _viewer_id())
        if host is None:
            raise AppError("User not found.", 404)
        event = events_dao.create_event(
            db, host,
            title=body.get("title"),
            starts_at=_parse_time(body.get("startsAt"), "startsAt"),
            ends_at=_parse_time(body.get("endsAt"), "endsAt"),
            description=body.get("description"),
            cover_media_url=body.get("coverMediaUrl"),
            category=body.get("category"),
            tz=body.get("timezone"),
            venue_name=body.get("venueName"),
            address=body.get("address"),
            latitude=body.get("latitude"),
            longitude=body.get("longitude"),
            capacity=body.get("capacity"),
        )
        return event_to_dict(db, event, viewer_id=host.id), 201
    finally:
        db.close()


def patch(event_id: str):
    body = request.get_json() or {}
    db = SessionLocal()
    try:
        event, viewer_id = _require_host(db, event_id)
        if event.status in event_state.TERMINAL_STATUSES:
            raise AppError("This event can no longer be edited.", 409)

        if "title" in body:
            event.title = events_dao.validate_title(body.get("title"))
        if "description" in body:
            event.description = (body.get("description") or "").strip() or None
        if "coverMediaUrl" in body:
            event.cover_media_url = body.get("coverMediaUrl")
        if "category" in body:
            event.category = events_dao.validate_category(body.get("category"))
        if "timezone" in body:
            event.timezone = body.get("timezone")
        if "venueName" in body:
            event.venue_name = (body.get("venueName") or "").strip() or None
        if "address" in body:
            event.address = (body.get("address") or "").strip() or None
        if "latitude" in body:
            event.latitude = body.get("latitude")
        if "longitude" in body:
            event.longitude = body.get("longitude")
        if "capacity" in body:
            event.capacity = events_dao.validate_capacity(db, event, body.get("capacity"))

        if "startsAt" in body or "endsAt" in body:
            starts_at = (
                _parse_time(body["startsAt"], "startsAt") if "startsAt" in body
                else event.starts_at
            )
            ends_at = (
                _parse_time(body["endsAt"], "endsAt") if "endsAt" in body else event.ends_at
            )
            events_dao.validate_window(starts_at, ends_at)
            if event.status == EVENT_PUBLISHED:
                # A published event's auctions take their window from these times, and they
                # are already open with bids against them. Moving the clock under a running
                # auction is a product decision nobody has made, so it is refused rather
                # than half-applied.
                raise AppError(
                    "A published event's date can't be changed. Cancel it and create a new "
                    "one if the date has moved.",
                    409,
                )
            event.starts_at = starts_at
            event.ends_at = ends_at

        db.commit()
        return event_to_dict(db, event, viewer_id=viewer_id), 200
    finally:
        db.close()


def set_people(event_id: str):
    """Replace the cohost or artist list.

    Keyed on usernames rather than ids because that is what the app has: the people picker
    returns handles, the response returns handles, and profiles are addressed by handle.
    Taking ids here would force the client into a lookup round trip purely to satisfy this
    one endpoint.

    A role that is present is replaced wholesale; a role that is absent is left alone. Both
    matter: replacing is the only way to remove somebody from the one screen that exists,
    and leaving absent roles alone means setting artists does not silently clear cohosts.
    """
    body = request.get_json() or {}
    db = SessionLocal()
    try:
        event, viewer_id = _require_host(db, event_id)
        for key, role in (("cohostUsernames", ROLE_COHOST), ("artistUsernames", ROLE_ARTIST)):
            if key not in body:
                continue
            handles = [
                str(h).lstrip("@").strip().lower()
                for h in (body.get(key) or [])
                if str(h).strip()
            ]
            found = {
                user.username.lower(): user.id
                for user in db.query(User).filter(
                    func.lower(User.username).in_(handles)
                ).all()
            } if handles else {}
            missing = [h for h in handles if h not in found]
            if missing:
                raise AppError(f"No account found for @{missing[0]}.", 404)
            events_dao.set_participants(
                db, event, role=role, user_ids=[found[h] for h in handles], commit=False
            )
        db.commit()
        return event_to_dict(db, event, viewer_id=viewer_id), 200
    finally:
        db.close()


def publish(event_id: str):
    """Put the event out, and list everything on its bill.

    The two happen together and commit together. A published event whose lineup failed to
    list would be an event advertising work nobody can buy.
    """
    db = SessionLocal()
    try:
        event, viewer_id = _require_host(db, event_id)
        if event.status == EVENT_PUBLISHED:
            return event_to_dict(db, event, viewer_id=viewer_id), 200
        if event.ends_at <= datetime.now(timezone.utc):
            raise AppError("This event has already ended.", 409)

        event_state.transition_event(
            db, event, EVENT_PUBLISHED, allowed_from={"draft"},
            reason="host_published", commit=False,
        )
        event.published_at = datetime.now(timezone.utc)
        listed = lineup_service.publish_lineup(db, event, commit=False)
        db.commit()

        result = event_to_dict(db, event, viewer_id=viewer_id)
        result.update(listed)
        return result, 200
    finally:
        db.close()


def cancel(event_id: str):
    """Call the event off, and take down everything it listed."""
    body = request.get_json() or {}
    reason = (body.get("reason") or "").strip()[:200] or "Event cancelled"
    db = SessionLocal()
    try:
        event, viewer_id = _require_host(db, event_id)
        event_state.transition_event(
            db, event, EVENT_CANCELLED, reason="host_cancelled", commit=False
        )
        event.cancellation_reason = reason
        undone = lineup_service.cancel_lineup(db, event, "event_cancelled", commit=False)
        db.commit()

        # After the commit, and the reason this matters: cancelling used to take down the
        # listings and tell the bidders, while the people who had arranged their evening
        # around turning up heard nothing at all.
        told = rsvp_service.notify_attendees(
            db, event,
            type="event_cancelled",
            title="Event cancelled",
            body=f'"{event.title}" has been cancelled. {reason}',
        )

        # Cancelling takes down listings, refunds bidders and stands people up. Worth a
        # durable record of who did it and what it cost.
        audit_service.record(
            db, audit.AUDIT_EVENT_CANCELLED,
            actor=get_user_by_id(db, viewer_id),
            subject_type="event", subject_id=event.id,
            detail={**undone, "attendeesNotified": told},
            note=reason,
        )

        result = event_to_dict(db, event, viewer_id=viewer_id)
        result.update(undone)
        result["attendeesNotified"] = told
        return result, 200
    finally:
        db.close()


def delete_event(event_id: str):
    """Remove a hosted event outright.

    A draft is nobody's problem but the host's — it is deleted with nothing else to do. A
    published event has to be taken off first: its listings released, any auction cancelled
    and refunded, its attendees told the event isn't happening — the same work `cancel` does —
    before the row itself is removed, so nobody who RSVP'd or was bidding just finds it gone.

    Title and id are read into locals before the row is deleted: the session expires an
    object's attributes on commit, and re-reading one off a row that is no longer there is
    exactly the `event.title` access the audit call below would otherwise make.
    """
    db = SessionLocal()
    try:
        event, viewer_id = _require_host(db, event_id)
        event_uuid = event.id
        event_title = event.title
        was_published = event.status == EVENT_PUBLISHED
        undone = {"cancelledAuctions": 0, "delisted": 0}
        told = 0

        if was_published:
            event_state.transition_event(
                db, event, EVENT_CANCELLED, reason="host_deleted", commit=False
            )
            event.cancellation_reason = "Event deleted by host"
            undone = lineup_service.cancel_lineup(db, event, "event_deleted", commit=False)
            db.commit()
            told = rsvp_service.notify_attendees(
                db, event,
                type="event_cancelled",
                title="Event cancelled",
                body=f'"{event_title}" has been removed by its host.',
            )

        events_dao.delete_event(db, event)

        audit_service.record(
            db, audit.AUDIT_EVENT_DELETED,
            actor=get_user_by_id(db, viewer_id),
            subject_type="event", subject_id=event_uuid,
            detail={**undone, "attendeesNotified": told, "wasPublished": was_published},
            note=f'"{event_title}" deleted by host',
        )

        return {"deleted": True}, 200
    finally:
        db.close()


# --- host: the bill ---------------------------------------------------------------------------

def add_lineup_piece(event_id: str):
    body = request.get_json() or {}
    db = SessionLocal()
    try:
        viewer_id = _viewer_id()
        event = events_dao.get_event(db, _uuid(event_id, "event"))
        if not event:
            raise AppError("Event not found.", 404)
        actor = get_user_by_id(db, viewer_id)
        if actor is None:
            raise AppError("User not found.", 404)

        piece = get_piece(db, _uuid(body.get("pieceId"), "piece"))
        if not piece:
            raise AppError("Piece not found.", 404)

        # The host runs the bill; an artist may add their own work to an event they are on.
        is_host = event.host_id == viewer_id
        is_billed = any(
            row.EventParticipant.user_id == viewer_id
            for row in events_dao.list_participants(db, event.id)
        )
        if not is_host and not (is_billed and piece.user_id == viewer_id):
            raise AppError("Event not found.", 404)

        entry = lineup_service.add_piece(
            db, event, piece, actor,
            mode=body.get("mode") or PIECE_FEATURED,
            price_cents=body.get("priceCents"),
            delivery_mode=body.get("deliveryMode"),
            sort_order=body.get("sortOrder") or 0,
        )
        return lineup_entry_to_dict(db, entry), 201
    finally:
        db.close()


def remove_lineup_piece(event_id: str, piece_id: str):
    db = SessionLocal()
    try:
        viewer_id = _viewer_id()
        event = events_dao.get_event(db, _uuid(event_id, "event"))
        if not event:
            raise AppError("Event not found.", 404)
        entry = db.query(EventPiece).filter_by(
            event_id=event.id, piece_id=_uuid(piece_id, "piece")
        ).one_or_none()
        if entry is None:
            raise AppError("That piece isn't on this bill.", 404)

        piece = db.get(Piece, entry.piece_id)
        # The host can take anything off the bill; an artist can withdraw their own work.
        if event.host_id != viewer_id and (piece is None or piece.user_id != viewer_id):
            raise AppError("Event not found.", 404)

        lineup_service.remove_piece(db, event, entry)
        return {"removed": True}, 200
    finally:
        db.close()


def preview_piece_tagging(event_id: str, piece_id: str):
    """What adding this piece would end. Read-only, for the confirmation dialog."""
    db = SessionLocal()
    try:
        piece = get_piece(db, _uuid(piece_id, "piece"))
        if not piece:
            raise AppError("Piece not found.", 404)
        result = lineup_service.preview_tagging(db, piece)
        result["ownedByViewer"] = piece.user_id == _viewer_id()
        return result, 200
    finally:
        db.close()


# --- public: browse and detail ------------------------------------------------------------

def browse():
    """The events tab: what's on today, from people you follow, and everything upcoming."""
    db = SessionLocal()
    try:
        viewer_id = _viewer_id()
        today = events_dao.list_today(db)
        upcoming = events_dao.list_upcoming(db)
        following = (
            events_dao.list_from_following(db, viewer_id) if viewer_id else []
        )
        return {
            "today": [event_card(db, e, viewer_id) for e in today],
            "following": [event_card(db, e, viewer_id) for e in following],
            "upcoming": [event_card(db, e, viewer_id) for e in upcoming],
            "categories": events_dao.category_summaries(db),
        }, 200
    finally:
        db.close()


def list_events():
    db = SessionLocal()
    try:
        viewer_id = _viewer_id()
        category = request.args.get("category")
        scope = (request.args.get("scope") or "upcoming").lower()
        limit = _int_arg("limit", events_dao.DEFAULT_LIMIT)
        offset = _int_arg("offset", 0)

        if scope == "saved":
            if viewer_id is None:
                raise AppError("Sign in to see your saved events.", 401)
            events = events_dao.list_saved(db, viewer_id, limit=limit)
        elif scope == "following":
            if viewer_id is None:
                raise AppError("Sign in to see events from people you follow.", 401)
            events = events_dao.list_from_following(db, viewer_id, limit=limit)
        elif scope == "today":
            events = events_dao.list_today(db, limit=limit)
        else:
            events = events_dao.list_upcoming(
                db, category=category, limit=limit, offset=offset
            )
        return {"events": [event_card(db, e, viewer_id) for e in events]}, 200
    finally:
        db.close()


def get_detail(event_id: str):
    db = SessionLocal()
    try:
        viewer_id = _viewer_id()
        event = events_dao.get_event(db, _uuid(event_id, "event"))
        if not event:
            raise AppError("Event not found.", 404)
        # A draft is the host's private working copy.
        if event.status == "draft" and event.host_id != viewer_id:
            raise AppError("Event not found.", 404)
        return event_to_dict(db, event, viewer_id=viewer_id), 200
    finally:
        db.close()


def set_saved(event_id: str):
    body = request.get_json() or {}
    saved = bool(body.get("saved", True))
    db = SessionLocal()
    try:
        viewer_id = _viewer_id()
        event = events_dao.get_event(db, _uuid(event_id, "event"))
        if not event:
            raise AppError("Event not found.", 404)
        events_dao.set_saved(db, event.id, viewer_id, saved)
        return {"saved": saved, "saveCount": events_dao.count_saves(db, event.id)}, 200
    finally:
        db.close()


def list_for_host(username: str):
    from src.modules.user.user_dao import get_user_by_username

    db = SessionLocal()
    try:
        host = get_user_by_username(db, username)
        if host is None:
            raise AppError("User not found.", 404)
        viewer_id = _viewer_id()
        events = events_dao.list_for_host(db, host.id, viewer_id=viewer_id)
        return {"events": [event_card(db, e, viewer_id) for e in events]}, 200
    finally:
        db.close()


def set_rsvp(event_id: str):
    """Say you're coming, or take it back. Free — an RSVP is a headcount, not a ticket."""
    body = request.get_json() or {}
    going = bool(body.get("going", True))
    db = SessionLocal()
    try:
        viewer_id = _viewer_id()
        user = get_user_by_id(db, viewer_id)
        if user is None:
            raise AppError("User not found.", 404)
        event = events_dao.get_event(db, _uuid(event_id, "event"))
        # A draft is 404 to anyone but its host, the same as reading one. Answering "this
        # event isn't open for RSVPs" would confirm the id belongs to a real unpublished
        # event, which is exactly what the 404 elsewhere exists to avoid.
        if not event or (event.status == "draft" and event.host_id != viewer_id):
            raise AppError("Event not found.", 404)

        rsvp_service.set_rsvp(db, event, user, going=going)
        return {
            "going": going,
            "rsvpCount": rsvp_service.going_count(db, event.id),
            "spotsLeft": rsvp_service.spots_left(db, event),
            "isFull": rsvp_service.is_full(db, event),
        }, 200
    finally:
        db.close()


def list_attendees(event_id: str):
    """Who is coming. Host-only: an attendee list is not public."""
    db = SessionLocal()
    try:
        event, _ = _require_host(db, event_id)
        people = rsvp_service.list_attendees(db, event.id)
        return {
            "attendees": [_person(user) for user in people],
            "rsvpCount": len(people),
            "capacity": event.capacity,
            "spotsLeft": rsvp_service.spots_left(db, event),
        }, 200
    finally:
        db.close()


def qr_codes(event_id: str):
    """The links a host prints and puts beside each work in the room.

    Built server-side rather than assembled in the app, so the code on the wall and the code
    the app resolves can never be two different opinions about what a share URL looks like.

    The QR carries a **link, not a token**. It is navigation: scanning it opens the piece so
    somebody can bid from where they are standing. It admits nobody and proves nothing, which
    is what lets a visitor photograph it, send it to a friend, and have that work too.
    """
    db = SessionLocal()
    try:
        event, viewer_id = _require_host(db, event_id)
        base = _share_base_url()
        entries = []
        for entry in lineup_service.list_lineup(db, event.id):
            piece = db.get(Piece, entry.piece_id)
            if piece is None:
                continue
            artist = db.get(User, piece.user_id)
            entries.append({
                "pieceId": str(entry.piece_id),
                "title": piece.title,
                "mode": entry.mode,
                "priceCents": entry.price_cents,
                "mediaUrl": piece.media_url,
                # A gallery card without the artist's name is missing the point of a
                # gallery card.
                "artistName": artist.name if artist else None,
                "artistUsername": artist.username if artist else None,
                # What the QR encodes. The /share/ route renders a preview for anyone who
                # opens it in a browser and hands the app the deep link when it is installed.
                "url": f"{base}/share/piece/{entry.piece_id}",
            })
        return {
            "eventId": str(event.id),
            "eventUrl": f"{base}/share/event/{event.id}",
            "pieces": entries,
        }, 200
    finally:
        db.close()


def _share_base_url() -> str:
    """The host that serves share links.

    BACKEND_URL rather than FRONTEND_URL: these links are served by this service, and during
    testing the production web domain is not pointed anywhere. Falling back to the request's
    own host keeps the codes working on whatever URL the app is actually reaching.
    """
    import os

    configured = (os.getenv("BACKEND_URL") or "").strip().rstrip("/")
    if configured:
        return configured
    return request.host_url.rstrip("/")


# --- serialization ---------------------------------------------------------------------------

def event_card(db, event: Event, viewer_id: Optional[uuid.UUID]) -> dict:
    """The compact shape every list renders. Deliberately cheap — no lineup, no participants."""
    host = db.get(User, event.host_id)
    return {
        "id": str(event.id),
        "title": event.title,
        "category": event.category,
        "status": event.status,
        "coverMediaUrl": event.cover_media_url,
        "startsAt": event.starts_at.isoformat() if event.starts_at else None,
        "endsAt": event.ends_at.isoformat() if event.ends_at else None,
        "timezone": event.timezone,
        "venueName": event.venue_name,
        # Entry is free and open — attending is never gated. Sent as a field rather than
        # left to the client to assume, so paid ticketing can change it in one place.
        "isFree": True,
        "hostUsername": host.username if host else None,
        "hostName": host.name if host else None,
        "hostAvatarUrl": host.image if host else None,
        "saved": events_dao.is_saved(db, event.id, viewer_id),
        "capacity": event.capacity,
        "rsvpCount": rsvp_service.going_count(db, event.id),
        "spotsLeft": rsvp_service.spots_left(db, event),
        "isFull": rsvp_service.is_full(db, event),
        "viewerIsGoing": _viewer_going(db, event.id, viewer_id),
    }


def event_to_dict(db, event: Event, viewer_id: Optional[uuid.UUID]) -> dict:
    base = event_card(db, event, viewer_id)
    base.update({
        "description": event.description,
        "address": event.address,
        "latitude": event.latitude,
        "longitude": event.longitude,
        "publishedAt": event.published_at.isoformat() if event.published_at else None,
        "cancellationReason": event.cancellation_reason,
        "isHost": event.host_id == viewer_id,
        "saveCount": events_dao.count_saves(db, event.id),
        "cohosts": [
            _person(user) for row, user in
            [(r.EventParticipant, r.User) for r in events_dao.list_participants(
                db, event.id, ROLE_COHOST)]
        ],
        "artists": [
            _person(user) for row, user in
            [(r.EventParticipant, r.User) for r in events_dao.list_participants(
                db, event.id, ROLE_ARTIST)]
        ],
        "lineup": [
            lineup_entry_to_dict(db, entry, viewer_id=viewer_id)
            for entry in lineup_service.list_lineup(db, event.id)
        ],
    })
    return base


def lineup_entry_to_dict(db, entry: EventPiece, viewer_id: Optional[uuid.UUID] = None) -> dict:
    """One work on the bill.

    A `bid` entry carries its live auction state — what it is at, how many bids, whether the
    viewer leads — because the screen people look at in the room is the event, not each piece
    in turn. Without it the lineup could only show a starting price, which stops being true
    the moment somebody bids and makes the room's own screen the least current thing in it.
    """
    from src.modules.bids import auction_dao, bid_dao
    from src.shared.models.event import PIECE_BID

    piece = db.get(Piece, entry.piece_id)
    artist = db.get(User, piece.user_id) if piece else None
    result = {
        "id": str(entry.id),
        "pieceId": str(entry.piece_id),
        "mode": entry.mode,
        "priceCents": entry.price_cents,
        "deliveryMode": entry.delivery_mode,
        "sortOrder": entry.sort_order,
        "title": piece.title if piece else None,
        "mediaUrl": piece.media_url if piece else None,
        "artistUsername": artist.username if artist else None,
        "artistName": artist.name if artist else None,
        "pieceStatus": piece.status if piece else None,
    }

    if entry.mode == PIECE_BID and piece is not None:
        auction = auction_dao.get_running_auction(db, piece.id) or (
            auction_dao.get_settling_auction(db, piece.id)
        )
        if auction is not None:
            result["auction"] = bid_dao.bid_summary(db, auction, viewer_id=viewer_id)
    return result


def _viewer_going(db, event_id, viewer_id) -> bool:
    rsvp = rsvp_service.viewer_rsvp(db, event_id, viewer_id)
    return rsvp is not None and rsvp.status == "going"


def _person(user: User) -> dict:
    return {
        "username": user.username,
        "name": user.name,
        "avatarUrl": user.image,
    }


def _int_arg(name: str, default: int) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise AppError(f"{name} must be a number.", 400) from None
