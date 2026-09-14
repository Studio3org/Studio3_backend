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
