"""Orders routes."""
from flask import Blueprint

from src.middlewares.auth_middleware import admin_required, auth_required
from src.shared.utils.api_response import success_response
from src.shared.utils.async_handler import async_handler
from src.modules.orders import orders_controller
from src.modules.payments import payments_controller
from src.modules.shipments import shipments_controller

orders_bp = Blueprint("orders", __name__)


def _ok(message, data, status=200):
    resp, _ = success_response(message, data, status)
    return resp, status


@orders_bp.get("/<order_id>")
@auth_required
@async_handler
def get_order(order_id):
    data, status = orders_controller.get_detail(order_id)
    return _ok("OK", data, status)


@orders_bp.post("/<order_id>/create-payment-intent")
@auth_required
@async_handler
def create_payment_intent(order_id):
    data, status = payments_controller.create_payment_intent(order_id)
    return _ok("Payment intent ready.", data, status)


@orders_bp.get("/<order_id>/payment-status")
@auth_required
@async_handler
def payment_status(order_id):
    data, status = payments_controller.payment_status(order_id)
    return _ok("OK", data, status)


@orders_bp.post("/<order_id>/confirm")
@auth_required
@async_handler
def confirm(order_id):
    # Kept for the existing client. With Stripe configured this only reports status (the
    # webhook decides payment); without it, it keeps the dev-mode auto-succeed behaviour.
    data, status = orders_controller.confirm(order_id)
    return _ok("OK", data, status)


@orders_bp.post("/<order_id>/confirm-received")
@auth_required
@async_handler
def confirm_received(order_id):
    data, status = orders_controller.confirm_received(order_id)
    return _ok("Delivery confirmed.", data, status)


@orders_bp.post("/<order_id>/report-issue")
@auth_required
@async_handler
def report_issue(order_id):
    data, status = orders_controller.report_issue(order_id)
    return _ok("Issue reported.", data, status)


@orders_bp.post("/<order_id>/shipment")
@admin_required
@async_handler
def create_shipment(order_id):
    data, status = shipments_controller.create(order_id)
    return _ok("Shipment recorded.", data, status)


@orders_bp.patch("/<order_id>/shipment")
@admin_required
@async_handler
def update_shipment(order_id):
    data, status = shipments_controller.update(order_id)
    return _ok("Shipment updated.", data, status)


@orders_bp.get("/<order_id>/shipment")
@auth_required
@async_handler
def get_shipment(order_id):
    data, status = shipments_controller.get(order_id)
    return _ok("OK", data, status)


@orders_bp.patch("/<order_id>")
@auth_required
@async_handler
def patch(order_id):
    data, status = orders_controller.patch(order_id)
    return _ok("Order updated.", data, status)
