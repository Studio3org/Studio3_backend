"""Admin console API routes — JSON, bearer-token auth, mounted at /api/admin.

Separate from `admin_routes.py`, which serves the HTML console at /admin with cookie
sessions. Same operations, different caller: these are for the web app's admin screens.

Every route is `admin_required`, which is the JWT gate. Authorization is the `is_admin` flag
alone — `User.role` is a marketing category and must never grant access to anything here.
"""
from flask import Blueprint

from src.middlewares.auth_middleware import admin_required
from src.shared.utils.api_response import success_response
from src.shared.utils.async_handler import async_handler
from src.modules.admin import admin_api_controller as ctrl

admin_api_bp = Blueprint("admin_api", __name__)


def _ok(message, data, status=200):
    resp, _ = success_response(message, data, status)
    return resp, status


@admin_api_bp.get("/summary")
@admin_required
@async_handler
def summary():
    data, status = ctrl.summary()
    return _ok("Admin summary.", data, status)


# --- orders ------------------------------------------------------------------------------

@admin_api_bp.get("/orders")
@admin_required
@async_handler
def list_orders():
    data, status = ctrl.list_orders()
    return _ok("Orders.", data, status)


@admin_api_bp.get("/orders/<order_id>")
@admin_required
@async_handler
def get_order(order_id):
    data, status = ctrl.get_order(order_id)
    return _ok("Order.", data, status)


@admin_api_bp.post("/orders/<order_id>/shipment")
@admin_required
@async_handler
def create_shipment(order_id):
    data, status = ctrl.create_shipment(order_id)
    return _ok("Shipment recorded.", data, status)


@admin_api_bp.post("/orders/<order_id>/shipment/update")
@admin_required
@async_handler
def update_shipment(order_id):
    data, status = ctrl.update_shipment(order_id)
    return _ok("Shipment updated.", data, status)


@admin_api_bp.post("/orders/<order_id>/resolve")
@admin_required
@async_handler
def resolve_dispute(order_id):
    data, status = ctrl.resolve_dispute(order_id)
    return _ok(data.get("notice", "Dispute resolved."), data, status)


@admin_api_bp.post("/orders/<order_id>/retry-payout")
@admin_required
@async_handler
def retry_payout(order_id):
    data, status = ctrl.retry_payout(order_id)
    return _ok(data.get("notice", "Payout released."), data, status)


# --- queues ------------------------------------------------------------------------------

@admin_api_bp.get("/disputes")
@admin_required
@async_handler
def list_disputes():
    data, status = ctrl.list_disputes()
    return _ok("Disputes.", data, status)


@admin_api_bp.get("/auctions")
@admin_required
@async_handler
def list_auctions():
    data, status = ctrl.list_auctions()
    return _ok("Auctions.", data, status)


@admin_api_bp.get("/events")
@admin_required
@async_handler
def list_events():
    data, status = ctrl.list_events()
    return _ok("Events.", data, status)


@admin_api_bp.get("/audit")
@admin_required
@async_handler
def audit_log():
    data, status = ctrl.audit_log()
    return _ok("Audit log.", data, status)


# --- reports -----------------------------------------------------------------------------

@admin_api_bp.get("/reports")
@admin_required
@async_handler
def list_reports():
    data, status = ctrl.list_reports()
    return _ok("Reports.", data, status)


@admin_api_bp.post("/reports/<report_id>/resolve")
@admin_required
@async_handler
def resolve_report(report_id):
    data, status = ctrl.resolve_report(report_id)
    return _ok("Report resolved.", data, status)
