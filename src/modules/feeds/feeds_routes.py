"""Feed routes."""
from flask import Blueprint

from src.middlewares.auth_middleware import onboarding_required, optional_auth
from src.shared.utils.api_response import success_response
from src.shared.utils.async_handler import async_handler
from src.modules.feeds import feeds_controller

feeds_bp = Blueprint("feeds", __name__)


def _ok(message, data, status=200):
    resp, _ = success_response(message, data, status)
    return resp, status


@feeds_bp.get("/following")
@onboarding_required
@async_handler
def following():
    data, status = feeds_controller.following_feed()
    return _ok("OK", data, status)


@feeds_bp.get("/explore")
@optional_auth
@async_handler
def explore():
    data, status = feeds_controller.explore_feed()
    return _ok("OK", data, status)


@feeds_bp.get("/for-you")
@onboarding_required
@async_handler
def for_you():
    data, status = feeds_controller.for_you_feed()
    return _ok("OK", data, status)
