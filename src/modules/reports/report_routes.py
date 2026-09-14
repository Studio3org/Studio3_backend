"""Report routes — flag a piece, post, or user for moderation review."""
from flask import Blueprint

from src.middlewares.auth_middleware import auth_required
from src.shared.utils.api_response import success_response
from src.shared.utils.async_handler import async_handler
from src.modules.reports import report_controller

reports_bp = Blueprint("reports", __name__)


def _ok(message, data, status=200):
    resp, _ = success_response(message, data, status)
    return resp, status


@reports_bp.post("/pieces/<piece_id>/report")
@auth_required
@async_handler
def report_piece(piece_id):
    data, status = report_controller.report_piece(piece_id)
    return _ok("Thanks — we received your report.", data, status)


@reports_bp.post("/posts/<post_id>/report")
@auth_required
@async_handler
def report_post(post_id):
    data, status = report_controller.report_post(post_id)
    return _ok("Thanks — we received your report.", data, status)


@reports_bp.post("/users/<username>/report")
@auth_required
@async_handler
def report_user(username):
    data, status = report_controller.report_user(username)
    return _ok("Thanks — we received your report.", data, status)


@reports_bp.get("/reports/mine")
@auth_required
@async_handler
def list_my_reports():
    data, status = report_controller.list_my_reports()
    return _ok("OK", data, status)
