"""Shipment endpoints — admin-only writes, participant-visible reads."""
import uuid
from datetime import datetime

from flask import g, request

from src.shared.config.database import SessionLocal
from src.shared.models.piece import Piece
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.orders import orders_dao
from src.modules.notifications import notifications_dao
from src.modules.shipments import shipments_dao

logger = get_logger(__name__)


def _parse_cost(raw) -> int:
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise AppError("actualShippingCostCents must be a whole number of cents.", 400)
    if value < 0:
        raise AppError("actualShippingCostCents cannot be negative.", 400)
    return value


def create(order_id: str):
    body = request.get_json() or {}
    admin_id = uuid.UUID(g.user["id"])
    db = SessionLocal()
    try:
        order = orders_dao.get_order_for_update(db, uuid.UUID(order_id))
        if not order:
            raise AppError("Order not found.", 404)
        shipment = shipments_dao.create_shipment(
            db,
            order,
            courier=body.get("courier"),
            tracking_number=body.get("trackingNumber"),
            admin_id=admin_id,
            actual_cost_cents=_parse_cost(body.get("actualShippingCostCents")),
        )
        _notify_shipped(db, order, shipment)
        return shipments_dao.shipment_to_dict(shipment), 201
    finally:
        db.close()


def update(order_id: str):
    body = request.get_json() or {}
    db = SessionLocal()
    try:
        order = orders_dao.get_order_for_update(db, uuid.UUID(order_id))
        if not order:
            raise AppError("Order not found.", 404)
        previous_status = order.status
        shipment = shipments_dao.update_shipment(
            db,
            order,
            status=body.get("status"),
            courier=body.get("courier"),
            tracking_number=body.get("trackingNumber"),
            actual_cost_cents=_parse_cost(body.get("actualShippingCostCents")),
        )
        if order.status == "awaiting_confirmation" and previous_status != "awaiting_confirmation":
            _notify_delivered(db, order)
        return shipments_dao.shipment_to_dict(shipment), 200
    finally:
        db.close()


def get(order_id: str):
    """Tracking is visible to the collector, the artist, and admins (FR-4.4)."""
    db = SessionLocal()
    try:
        user_id = uuid.UUID(g.user["id"])
        order = orders_dao.get_order(db, uuid.UUID(order_id))
        if not order:
            raise AppError("Order not found.", 404)
        viewer = db.get(User, user_id)
        is_participant = user_id in (order.buyer_id, order.seller_id)
        if not is_participant and not (viewer and viewer.is_admin):
            raise AppError("Order not found.", 404)
        return {"shipment": shipments_dao.shipment_to_dict(shipments_dao.get_shipment(db, order.id))}, 200
    finally:
        db.close()


def _piece_title(db, order):
    items = orders_dao.list_items(db, order.id)
    piece = db.get(Piece, items[0].piece_id) if items else None
    return piece.title if piece else None


def _notify_shipped(db, order, shipment) -> None:
    title = _piece_title(db, order)
    payload = {
        "pieceTitle": title,
        "courier": shipment.courier,
        "trackingNumber": shipment.tracking_number,
    }
    try:
        for user_id, body in (
            (order.buyer_id, f"Your order is on its way via {shipment.courier}"),
            (order.seller_id, f"The artwork was collected by {shipment.courier}"),
        ):
            notifications_dao.create_and_push(
                db, user_id=user_id, type="order", actor_id=None, target_type="order",
                target_id=order.id, payload=payload, title="Order shipped", body=body,
            )
    except Exception as e:
        logger.warning("Shipment notification failed for order %s: %s", order.id, e)


def _notify_delivered(db, order) -> None:
    title = _piece_title(db, order)
    try:
        notifications_dao.create_and_push(
            db, user_id=order.buyer_id, type="order", actor_id=None, target_type="order",
            target_id=order.id, payload={"pieceTitle": title},
            title="Your artwork has arrived",
            # The artist isn't paid until this confirmation, so say so plainly.
            body="Confirm you received it in good condition to release payment to the artist.",
        )
    except Exception as e:
        logger.warning("Delivery notification failed for order %s: %s", order.id, e)
