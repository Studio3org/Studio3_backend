"""Payments routes: webhook (unauthenticated, signature-verified) lives here."""
from flask import Blueprint, jsonify

from src.shared.utils.async_handler import async_handler
from src.modules.payments import payments_controller

payments_bp = Blueprint("payments", __name__)


@payments_bp.post("/webhook")
@async_handler
def webhook():
    # Raw dict, not the standard success_response envelope — Stripe only cares about the
    # status code, and this endpoint is not a client-facing API.
    data, status = payments_controller.webhook()
    return jsonify(data), status
