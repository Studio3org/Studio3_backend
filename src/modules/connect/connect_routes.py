"""Stripe Connect routes (artist-facing)."""
from flask import Blueprint

from src.middlewares.auth_middleware import seller_required
from src.shared.utils.api_response import success_response
from src.shared.utils.async_handler import async_handler
from src.modules.connect import connect_controller

connect_bp = Blueprint("connect", __name__)


def _ok(message, data, status=200):
    resp, _ = success_response(message, data, status)
    return resp, status


@connect_bp.post("/connect")
@seller_required
@async_handler
def start_onboarding():
    data, status = connect_controller.start_onboarding()
    return _ok("Onboarding link created.", data, status)


@connect_bp.get("/connect/status")
@seller_required
@async_handler
def onboarding_status():
    data, status = connect_controller.onboarding_status()
    return _ok("OK", data, status)


@connect_bp.get("/connect/dashboard")
@seller_required
@async_handler
def dashboard_link():
    data, status = connect_controller.dashboard_link()
    return _ok("OK", data, status)
