"""Admin UI routes — HTML, session-cookie auth, mounted at /admin (outside /api)."""
from flask import Blueprint

from src.modules.admin import admin_controller
from src.modules.admin.admin_auth import admin_session_required

admin_bp = Blueprint("admin", __name__, template_folder="../../templates")


@admin_bp.get("/login")
def login_form():
    return admin_controller.login_form()


@admin_bp.post("/login")
def login():
    return admin_controller.login()


@admin_bp.post("/logout")
@admin_session_required
def logout():
    return admin_controller.logout()


@admin_bp.get("/")
@admin_bp.get("/orders")
@admin_session_required
def orders_list():
    return admin_controller.orders_list()


@admin_bp.get("/orders/<order_id>")
@admin_session_required
def order_detail(order_id):
    return admin_controller.order_detail(order_id)


@admin_bp.get("/disputes")
@admin_session_required
def disputes_queue():
    return admin_controller.disputes_queue()


@admin_bp.get("/reports")
@admin_session_required
def reports_queue():
    return admin_controller.reports_queue()


@admin_bp.post("/reports/<report_id>/resolve")
@admin_session_required
def resolve_report(report_id):
    return admin_controller.resolve_report(report_id)


@admin_bp.post("/orders/<order_id>/shipment")
@admin_session_required
def create_shipment(order_id):
    return admin_controller.create_shipment(order_id)


@admin_bp.post("/orders/<order_id>/shipment/update")
@admin_session_required
def update_shipment(order_id):
    return admin_controller.update_shipment(order_id)


@admin_bp.post("/orders/<order_id>/resolve")
@admin_session_required
def resolve_dispute(order_id):
    return admin_controller.resolve_dispute(order_id)


@admin_bp.post("/orders/<order_id>/retry-payout")
@admin_session_required
def retry_payout(order_id):
    return admin_controller.retry_payout(order_id)


# --- the surfaces the last six phases added ------------------------------------------------
# Auctions, events and the audit trail. None of these existed in ops until now: auction work
# has been shipping since Phase 3 with no queue anyone could watch.

@admin_bp.get("/auctions")
@admin_session_required
def auctions_queue():
    return admin_controller.auctions_queue()


@admin_bp.get("/events")
@admin_session_required
def events_queue():
    return admin_controller.events_queue()


@admin_bp.get("/audit")
@admin_session_required
def audit_log():
    return admin_controller.audit_log()
