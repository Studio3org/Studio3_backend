"""User and media routes."""
import uuid

from flask import Blueprint, Response, abort, g, request

from src.middlewares.auth_middleware import auth_required, optional_auth
from src.shared.storage.local_storage import guess_content_type, read_local_file, save_local_file
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
    """Dev-only stand-in for an S3 presigned PUT when no bucket is configured."""
    save_local_file(key, request.get_data())
    return "", 204


@media_bp.get("/local/<path:key>")
def download_local_media(key):
    """Dev-only stand-in for a public S3 object URL."""
    data = read_local_file(key)
    if data is None:
        abort(404)
    return Response(data, mimetype=guess_content_type(key))
