"""Public user content listings (pieces, posts)."""
from flask import Blueprint, request

from src.middlewares.auth_middleware import optional_auth
from src.shared.utils.api_response import success_response
from src.shared.utils.async_handler import async_handler
from src.modules.events import events_controller
from src.modules.pieces import pieces_controller
from src.modules.posts import posts_controller
from src.modules.series import series_controller
from src.modules.social import social_controller
from src.modules.user import collected, user_controller

users_bp = Blueprint("users", __name__)


def _ok(message, data, status=200):
    resp, _ = success_response(message, data, status)
    return resp, status


@users_bp.get("/nearby")
@optional_auth
@async_handler
def nearby():
    data, status = user_controller.nearby_users()
    return _ok("OK", data, status)


@users_bp.get("/<username>/pieces")
@optional_auth
@async_handler
def list_pieces(username):
    data, status = pieces_controller.list_for_user(username)
    return _ok("OK", data, status)


@users_bp.get("/<username>/pieces/for-sale")
@optional_auth
@async_handler
def list_pieces_for_sale(username):
    data, status = pieces_controller.list_for_user(username, for_sale_only=True)
    return _ok("OK", data, status)


@users_bp.get("/<username>/posts")
@optional_auth
@async_handler
def list_posts(username):
    data, status = posts_controller.list_for_user(username)
    return _ok("OK", data, status)


@users_bp.get("/<username>/events")
@optional_auth
@async_handler
def list_events(username):
    data, status = events_controller.list_for_host(username)
    return _ok("OK", data, status)


@users_bp.get("/<username>/collection")
@optional_auth
@async_handler
def list_collection(username):
    """The pieces they have bought on Studio 3, for the profile's Collection tab (B37)."""
    data, status = collected.list_for_user(username)
    return _ok("OK", data, status)


@users_bp.get("/<username>/series")
@optional_auth
@async_handler
def list_series(username):
    data, status = series_controller.list_for_user(username)
    return _ok("OK", data, status)


@users_bp.get("/<username>/followers")
@optional_auth
@async_handler
def list_followers(username):
    cursor = request.args.get("cursor")
    limit = min(int(request.args.get("limit", 20)), 50)
    data, status = social_controller.list_followers(username, cursor=cursor, limit=limit)
    return _ok("OK", data, status)


@users_bp.get("/<username>/following")
@optional_auth
@async_handler
def list_following(username):
    cursor = request.args.get("cursor")
    limit = min(int(request.args.get("limit", 20)), 50)
    data, status = social_controller.list_following(username, cursor=cursor, limit=limit)
    return _ok("OK", data, status)
