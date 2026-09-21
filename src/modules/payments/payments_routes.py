"""Payments routes: the webhook (unauthenticated, signature-verified) and saved cards."""
from flask import Blueprint, jsonify

from src.middlewares.auth_middleware import auth_required
from src.shared.utils.api_response import success_response
from src.shared.utils.async_handler import async_handler
from src.modules.payments import payment_methods_controller, payments_controller

payments_bp = Blueprint("payments", __name__)


@payments_bp.post("/webhook")
@async_handler
def webhook():
    # Raw dict, not the standard success_response envelope — Stripe only cares about the
    # status code, and this endpoint is not a client-facing API.
    data, status = payments_controller.webhook()
    return jsonify(data), status


def _ok(message, data, status=200):
    resp, _ = success_response(message, data, status)
    return resp, status


# --- saved cards --------------------------------------------------------------------------
# Bidding needs a card on file before the first bid: a hold is authorised when the bid lands
# and re-authorised weeks later with nobody present, so there is no moment at which the
# bidder could be asked for it. Card data never reaches this server — Stripe's SetupIntent
# flow vaults it from the device.

@payments_bp.post("/setup-intent")
@auth_required
@async_handler
def create_setup_intent():
    data, status = payment_methods_controller.create_setup_intent()
    return _ok("Setup intent ready.", data, status)


@payments_bp.get("/payment-methods")
@auth_required
@async_handler
def list_payment_methods():
    data, status = payment_methods_controller.list_payment_methods()
    return _ok("OK", data, status)


@payments_bp.delete("/payment-methods/<payment_method_id>")
@auth_required
@async_handler
def detach_payment_method(payment_method_id):
    data, status = payment_methods_controller.detach_payment_method(payment_method_id)
    return _ok("Payment method removed.", data, status)
