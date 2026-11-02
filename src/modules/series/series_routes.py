"""Series routes."""
from flask import Blueprint

from src.middlewares.auth_middleware import onboarding_required
from src.shared.utils.api_response import success_response
from src.shared.utils.async_handler import async_handler
from src.modules.series import series_controller

series_bp = Blueprint("series", __name__)


def _ok(message, data, status=200):
    resp, _ = success_response(message, data, status)
    return resp, status


@series_bp.get("/<series_id>")
@async_handler
def get_series(series_id):
    data, status = series_controller.get_detail(series_id)
    return _ok("OK", data, status)


@series_bp.post("")
@onboarding_required
@async_handler
def create_series():
    data, status = series_controller.create()
    return _ok("Series created.", data, status)


@series_bp.patch("/<series_id>")
@onboarding_required
@async_handler
def patch_series(series_id):
    data, status = series_controller.patch(series_id)
    return _ok("Series updated.", data, status)


@series_bp.delete("/<series_id>")
@onboarding_required
@async_handler
def delete_series(series_id):
    data, status = series_controller.delete(series_id)
    return _ok("Series deleted.", data, status)


@series_bp.post("/<series_id>/pieces")
@onboarding_required
@async_handler
def add_piece(series_id):
    data, status = series_controller.add_piece(series_id)
    return _ok("Piece added to series.", data, status)


@series_bp.delete("/<series_id>/pieces/<piece_id>")
@onboarding_required
@async_handler
def remove_piece(series_id, piece_id):
    data, status = series_controller.remove_piece(series_id, piece_id)
    return _ok("Piece removed from series.", data, status)
