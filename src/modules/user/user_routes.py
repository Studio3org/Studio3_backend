"""User and media routes."""
import uuid

from flask import Blueprint, Response, abort, current_app, g, request

from src.middlewares.auth_middleware import auth_required, optional_auth
from src.shared.storage.local_storage import (
    guess_content_type,
    local_media_enabled,
    read_local_file,
    save_local_file,
    verify_upload,
)
from src.shared.storage.s3_service import ALLOWED_IMAGE, ALLOWED_VIDEO, VIDEO_MAX_BYTES
from src.shared.utils.api_response import success_response, apply_cookie_ops
from src.shared.utils.async_handler import async_handler
from src.modules.user import user_controller
from src.modules.media import media_controller
from src.modules.pieces import pieces_controller
from src.modules.addresses import addresses_controller
from src.modules.orders import orders_controller
from src.modules.posts import posts_controller
from src.modules.series import series_controller
from src.modules.auth import auth_controller

user_bp = Blueprint("user", __name__)
media_bp = Blueprint("media", __name__)


def _ok(message, data, status=200):
    resp, _ = success_response(message, data, status)
    return resp, status


@user_bp.get("/me")
@auth_required
@async_handler
def get_me():
    data, status = user_controller.get_me()
    return _ok("OK", data, status)


@user_bp.post("/me/accept-terms")
@auth_required
@async_handler
def accept_terms():
    data, status = user_controller.accept_terms()
    return _ok("Thanks for agreeing to the Terms of Use.", data, status)


@user_bp.patch("/me")
@auth_required
@async_handler
def patch_me():
    data, status = user_controller.patch_me()
    return _ok("Profile updated.", data, status)


@user_bp.patch("/me/username")
@auth_required
@async_handler
def patch_username():
    data, status = user_controller.patch_username()
    return _ok("Username updated.", data, status)


@user_bp.patch("/me/password")
@auth_required
@async_handler
def patch_password():
    data, status, cookie_ops = auth_controller.change_password()
    resp, _ = success_response("Password changed.", data, status)
    apply_cookie_ops(resp, cookie_ops)
    return resp, status


@user_bp.delete("/me")
@auth_required
@async_handler
def delete_account():
    data, status = auth_controller.delete_account()
    return _ok("Account deleted.", data, status)


@user_bp.post("/me/email/request-change")
@auth_required
@async_handler
def request_email_change():
    data, status, _ = auth_controller.request_email_change()
    return _ok("OK", data, status)


@user_bp.post("/me/email/confirm-change")
@auth_required
@async_handler
def confirm_email_change():
    data, status, _ = auth_controller.confirm_email_change()
    return _ok("Email updated.", data, status)


@user_bp.patch("/me/role")
@auth_required
@async_handler
def patch_role():
    data, status = user_controller.patch_role()
    return _ok("Role updated.", data, status)


@user_bp.post("/me/onboarding/preferences")
@auth_required
@async_handler
def onboarding_preferences():
    data, status = user_controller.onboarding_preferences()
    return _ok("Preferences saved.", data, status)


@user_bp.post("/me/onboarding/photos")
@auth_required
@async_handler
def onboarding_photos():
    data, status = user_controller.onboarding_photos()
    return _ok("Photos saved.", data, status)


@user_bp.post("/me/onboarding/complete")
@auth_required
@async_handler
def onboarding_complete():
    data, status = user_controller.onboarding_complete()
    return _ok("Onboarding complete.", data, status)


@user_bp.post("/me/seller/enable")
@auth_required
@async_handler
def seller_enable():
    data, status = user_controller.seller_enable()
    return _ok("Seller mode enabled.", data, status)


@user_bp.post("/me/seller/disable")
@auth_required
@async_handler
def seller_disable():
    data, status = user_controller.seller_disable()
    return _ok("Seller mode disabled.", data, status)


@user_bp.get("/me/seller")
@auth_required
@async_handler
def seller_status():
    data, status = user_controller.seller_status()
    return _ok("OK", data, status)


@user_bp.get("/me/seller/analytics")
@auth_required
@async_handler
def seller_analytics():
    data, status = user_controller.seller_analytics()
    return _ok("OK", data, status)


@user_bp.get("/me/commission-quote")
@auth_required
@async_handler
def commission_quote():
    data, status = user_controller.commission_quote()
    return _ok("OK", data, status)


@user_bp.get("/me/series")
@auth_required
@async_handler
def my_series():
    data, status = series_controller.list_for_me(uuid.UUID(g.user["id"]))
    return _ok("OK", data, status)


@user_bp.get("/me/saved/pieces")
@auth_required
@async_handler
def saved_pieces():
    data, status = pieces_controller.list_saved_for_me(uuid.UUID(g.user["id"]))
    return _ok("OK", data, status)


@user_bp.patch("/me/notification-preferences")
@auth_required
@async_handler
def patch_notification_preferences():
    data, status = user_controller.update_notification_preferences()
    return _ok("Notification preferences updated.", data, status)


@user_bp.post("/me/devices")
@auth_required
@async_handler
def register_device():
    data, status = user_controller.register_device()
    return _ok("Device registered.", data, status)


@user_bp.delete("/me/devices")
@auth_required
@async_handler
def unregister_device():
    data, status = user_controller.unregister_device()
    return _ok("Device unregistered.", data, status)


@user_bp.get("/me/addresses")
@auth_required
@async_handler
def list_addresses():
    data, status = addresses_controller.list_for_me()
    return _ok("OK", data, status)


@user_bp.post("/me/addresses")
@auth_required
@async_handler
def create_address():
    data, status = addresses_controller.create()
    return _ok("Address saved.", data, status)


@user_bp.patch("/me/addresses/<address_id>")
@auth_required
@async_handler
def patch_address(address_id):
    data, status = addresses_controller.patch(address_id)
    return _ok("Address updated.", data, status)


@user_bp.delete("/me/addresses/<address_id>")
@auth_required
@async_handler
def delete_address(address_id):
    data, status = addresses_controller.delete(address_id)
    return _ok("Address deleted.", data, status)


@user_bp.post("/me/addresses/<address_id>/default")
@auth_required
@async_handler
def set_default_address(address_id):
    data, status = addresses_controller.set_default(address_id)
    return _ok("Default address set.", data, status)


@user_bp.get("/me/orders")
@auth_required
@async_handler
def my_orders():
    data, status = orders_controller.list_my_orders()
    return _ok("OK", data, status)


@user_bp.get("/me/sales")
@auth_required
@async_handler
def my_sales():
    data, status = orders_controller.list_my_sales()
    return _ok("OK", data, status)


@user_bp.get("/me/drafts")
@auth_required
@async_handler
def my_drafts():
    """Piece and Scene drafts together, newest edit first (BACKEND_CHANGES B33)."""
    from src.modules.user import drafts

    data, status = drafts.list_for_me(uuid.UUID(g.user["id"]))
    return _ok("OK", data, status)


@user_bp.get("/me/tickets")
@auth_required
@async_handler
def my_tickets():
    """The viewer's tickets, optionally for one event (?eventId=). Each carries its QR value."""
    from src.modules.events import events_controller

    data, status = events_controller.my_tickets()
    return _ok("OK", data, status)


@user_bp.get("/me/pickup-address")
@auth_required
@async_handler
def get_pickup_address():
    """B31: the studio Convelio collects sold work from. Only ever shown to its owner."""
    from src.modules.shipping import shipping_controller

    data, status = shipping_controller.get_pickup_address()
    return _ok("OK", data, status)


@user_bp.put("/me/pickup-address")
@auth_required
@async_handler
def set_pickup_address():
    from src.modules.shipping import shipping_controller

    data, status = shipping_controller.set_pickup_address()
    return _ok("Pickup address saved.", data, status)


@user_bp.get("/me/deletion-check")
@auth_required
@async_handler
def deletion_check():
    """B34 SettingsDelete: what has to finish before this account can be deleted."""
    from src.modules.user import account_rules
    from src.shared.config.database import SessionLocal
    from src.shared.models.user import User

    db = SessionLocal()
    try:
        user = db.get(User, uuid.UUID(g.user["id"]))
        blockers = account_rules.deletion_blockers(db, user)
        return _ok("OK", {"canDelete": not blockers, "blockers": blockers}, 200)
    finally:
        db.close()


@user_bp.get("/me/seller-terms")
@auth_required
@async_handler
def seller_terms():
    """B34 SettingsSelling "Includes fees": this account's own rates and the terms they sell
    under. Rates come from the account (and settings), never from the app."""
    from src.shared.constants import CURRENT_TERMS_VERSION
    from src.shared.config import commission
    from src.shared.config.database import SessionLocal
    from src.shared.models.user import User

    db = SessionLocal()
    try:
        user = db.get(User, uuid.UUID(g.user["id"]))
        art = commission.resolve_bps(user, commission.SALE_ART)
        tickets = commission.resolve_bps(user, commission.SALE_TICKET)
        until = getattr(user, "commission_override_until", None)
        reduced = commission.override_active(user)
        return _ok("OK", {
            "artCommissionBps": art,
            "artCommissionPercent": art / 100,
            "ticketFeeBps": tickets,
            "ticketFeePercent": tickets / 100,
            "reducedRate": reduced,
            "reducedRateUntil": until.isoformat() if (reduced and until) else None,
            "buyersPayFees": False,
            "termsVersion": CURRENT_TERMS_VERSION,
            "termsUrl": "/terms",
        }, 200)
    finally:
        db.close()


@user_bp.get("/me/event-invites")
@auth_required
@async_handler
def my_event_invites():
    """Events I've been invited to as a co-host or featured artist, waiting for my answer."""
    from src.modules.events import events_controller

    data, status = events_controller.my_event_invites()
    return _ok("OK", data, status)


@user_bp.get("/me/piece-requests")
@auth_required
@async_handler
def my_piece_requests():
    """Hosts asking to show, sell or auction my pieces at their events."""
    from src.modules.events import events_controller

    data, status = events_controller.my_piece_requests()
    return _ok("OK", data, status)


@user_bp.get("/me/saved/posts")
@auth_required
@async_handler
def saved_posts():
    data, status = posts_controller.list_saved_for_me(uuid.UUID(g.user["id"]))
    return _ok("OK", data, status)


@user_bp.get("/<username>")
@optional_auth
@async_handler
def public_profile(username):
    data, status = user_controller.get_public_profile(username)
    return _ok("OK", data, status)


@media_bp.post("/presign")
@auth_required
@async_handler
def presign():
    data, status = media_controller.presign()
    return _ok("Presigned URL generated.", data, status)


@media_bp.put("/local/<path:key>")
def upload_local_media(key):
    """Dev-only stand-in for an S3 presigned PUT when no bucket is configured.

    Authorised like S3's: by the signed query string /presign handed out for this key, not by
    a login — the app PUTs to presigned URLs without its bearer token."""
    if not local_media_enabled():
        abort(404)
    if (request.content_length or 0) > VIDEO_MAX_BYTES:
        abort(413)
    if not verify_upload(
        current_app.secret_key, key, request.args, request.content_type, request.content_length
    ):
        abort(403)
    data = request.get_data()
    if len(data) > int(request.args["max"]):
        abort(413)
    try:
        save_local_file(key, data)
    except ValueError:
        abort(400)
    return "", 204


@media_bp.get("/local/<path:key>")
def download_local_media(key):
    """Dev-only stand-in for a public S3 object URL. Serves image and video types only, so the
    API origin can never be made to serve a page."""
    if not local_media_enabled():
        abort(404)
    content_type = guess_content_type(key)
    if content_type not in ALLOWED_IMAGE | ALLOWED_VIDEO:
        abort(404)
    try:
        data = read_local_file(key)
    except ValueError:
        abort(404)
    if data is None:
        abort(404)
    response = Response(data, mimetype=content_type)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# B32: hiding a piece from your own Collection (`/api/collection/<pieceId>/hide`).
collection_bp = Blueprint("collection", __name__)


@collection_bp.post("/<piece_id>/hide")
@auth_required
@async_handler
def hide_collected(piece_id):
    from src.modules.user import collected

    data, status = collected.set_hidden(piece_id, True)
    return _ok("Hidden from your Collection.", data, status)


@collection_bp.delete("/<piece_id>/hide")
@auth_required
@async_handler
def unhide_collected(piece_id):
    from src.modules.user import collected

    data, status = collected.set_hidden(piece_id, False)
    return _ok("Back in your Collection.", data, status)



# Phase 8: re-reading a shipping quote (`/api/shipping-quotes/<id>`).
shipping_quotes_bp = Blueprint("shipping_quotes", __name__)


@shipping_quotes_bp.get("/<quote_id>")
@auth_required
@async_handler
def get_shipping_quote(quote_id):
    from src.modules.shipping import shipping_controller

    data, status = shipping_controller.get_quote(quote_id)
    return _ok("OK", data, status)
