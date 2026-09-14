"""Pieces routes."""
import uuid

from flask import Blueprint, g

from src.middlewares.auth_middleware import onboarding_required, optional_auth, auth_required
from src.shared.utils.api_response import success_response
from src.shared.utils.async_handler import async_handler
from src.modules.pieces import pieces_controller

pieces_bp = Blueprint("pieces", __name__)


def _ok(message, data, status=200):
    resp, _ = success_response(message, data, status)
    return resp, status


@pieces_bp.post("")
@onboarding_required
@async_handler
def create_piece():
    data, status = pieces_controller.create()
    return _ok("Piece created.", data, status)


@pieces_bp.get("/<piece_id>")
@optional_auth
@async_handler
def get_piece(piece_id):
    viewer_id = uuid.UUID(g.user["id"]) if getattr(g, "user", None) else None
    data, status = pieces_controller.get_detail(piece_id, viewer_id)
    return _ok("OK", data, status)


@pieces_bp.patch("/<piece_id>")
@onboarding_required
@async_handler
def patch_piece(piece_id):
    data, status = pieces_controller.patch(piece_id)
    return _ok("Piece updated.", data, status)


@pieces_bp.delete("/<piece_id>")
@onboarding_required
@async_handler
def delete_piece(piece_id):
    data, status = pieces_controller.delete(piece_id)
    return _ok("Piece deleted.", data, status)


@pieces_bp.get("/<piece_id>/related-posts")
@async_handler
def related_posts(piece_id):
    from src.modules.posts import posts_controller
    data, status = posts_controller.related_for_piece(piece_id)
    return _ok("OK", data, status)


@pieces_bp.get("/<piece_id>/shipping-quote")
@optional_auth
@async_handler
def shipping_quote(piece_id):
    from src.modules.orders import orders_controller
    data, status = orders_controller.get_shipping_quote(piece_id)
    return _ok("OK", data, status)


@pieces_bp.post("/<piece_id>/collect")
@onboarding_required
@async_handler
def collect(piece_id):
    from src.modules.orders import orders_controller
    data, status = orders_controller.collect(piece_id)
    return _ok("Order created.", data, status)


@pieces_bp.post("/<piece_id>/bids")
@onboarding_required
@async_handler
def place_bid(piece_id):
    from src.modules.bids import bid_controller
    data, status = bid_controller.place_bid(piece_id)
    return _ok("Bid placed.", data, status)


@pieces_bp.post("/<piece_id>/auction-checkout")
@onboarding_required
@async_handler
def auction_checkout(piece_id):
    from src.modules.orders import orders_controller
    data, status = orders_controller.auction_checkout(piece_id)
    return _ok("Order created.", data, status)
