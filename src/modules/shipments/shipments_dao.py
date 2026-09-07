"""Shipment persistence + the order transitions shipment status drives.

Used by both the JSON API and the admin UI, so the two can never diverge on what "mark
delivered" means.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.shared.models.order import Order
from src.shared.models.shipment import (
    SHIPMENT_DELIVERED,
    SHIPMENT_IN_TRANSIT,
    SHIPMENT_LABEL_CREATED,
    SHIPMENT_PICKED_UP,
    Shipment,
)
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.orders import orders_dao
from src.modules.payments import money

logger = get_logger(__name__)

VALID_SHIPMENT_STATUSES = (
    SHIPMENT_LABEL_CREATED,
    SHIPMENT_PICKED_UP,
    SHIPMENT_IN_TRANSIT,
    SHIPMENT_DELIVERED,
)

# Couriers ops actually books with. Free text would make the tracking column unsearchable.
KNOWN_COURIERS = ("FedEx", "UPS", "DHL", "USPS", "Other")


def get_shipment(db: Session, order_id: uuid.UUID) -> Optional[Shipment]:
    return db.execute(
        select(Shipment).where(Shipment.order_id == order_id)
    ).scalar_one_or_none()


def shipment_to_dict(shipment: Optional[Shipment]) -> Optional[dict]:
    if not shipment:
        return None
    return {
        "id": str(shipment.id),
        "courier": shipment.courier,
        "trackingNumber": shipment.tracking_number,
        "shipmentDate": shipment.shipment_date.isoformat() if shipment.shipment_date else None,
        "status": shipment.status,
        "createdAt": shipment.created_at.isoformat(),
    }


def create_shipment(
    db: Session,
    order: Order,
    courier: str,
    tracking_number: str,
    admin_id: Optional[uuid.UUID] = None,
    actual_cost_cents: Optional[int] = None,
    shipment_date: Optional[datetime] = None,
) -> Shipment:
    """Record a booked courier pickup and move the order to shipped."""
    if get_shipment(db, order.id):
        raise AppError("This order already has a shipment.", 409)
    if order.status != "paid":
        raise AppError(f"Can't ship an order in status '{order.status}'.", 409)
    courier = (courier or "").strip()
    tracking_number = (tracking_number or "").strip()
    if not courier:
        raise AppError("Courier is required.", 400)
    if not tracking_number:
        raise AppError("Tracking number is required.", 400)

    shipment = Shipment(
        id=uuid.uuid4(),
        order_id=order.id,
        courier=courier,
        tracking_number=tracking_number,
        shipment_date=shipment_date or datetime.now(timezone.utc),
        status=SHIPMENT_PICKED_UP,
        actual_shipping_cost_cents=actual_cost_cents,
        created_by_admin_id=admin_id,
    )
    db.add(shipment)
    orders_dao.transition_order(db, order, "shipped", allowed_from={"paid"}, commit=False)
    if actual_cost_cents:
        money.book_shipping_cost(db, order, actual_cost_cents, commit=False)
    db.commit()
    db.refresh(shipment)
    logger.info("Shipment booked for order %s via %s.", order.id, courier)
    return shipment


def update_shipment(
    db: Session,
    order: Order,
    status: Optional[str] = None,
    courier: Optional[str] = None,
    tracking_number: Optional[str] = None,
    actual_cost_cents: Optional[int] = None,
) -> Shipment:
    """Update shipment details. Setting `delivered` also moves the order to
    awaiting_confirmation — there is no carrier webhook, so this manual step is what starts
    the collector's confirmation window (FR-4.5)."""
    shipment = get_shipment(db, order.id)
    if not shipment:
        raise AppError("No shipment exists for this order.", 404)

    if courier:
        shipment.courier = courier.strip()
    if tracking_number:
        shipment.tracking_number = tracking_number.strip()

    if actual_cost_cents is not None and actual_cost_cents != shipment.actual_shipping_cost_cents:
        if shipment.actual_shipping_cost_cents:
            # The ledger is append-only and this posting is keyed per order, so a correction
            # would need a reversing entry rather than an overwrite.
            raise AppError(
                "Shipping cost is already recorded and can't be changed here.", 409
            )
        shipment.actual_shipping_cost_cents = actual_cost_cents
        money.book_shipping_cost(db, order, actual_cost_cents, commit=False)

    if status:
        if status not in VALID_SHIPMENT_STATUSES:
            raise AppError(
                f"Status must be one of: {', '.join(VALID_SHIPMENT_STATUSES)}", 400
            )
        shipment.status = status
        if status == SHIPMENT_DELIVERED and order.status == "shipped":
            orders_dao.transition_order(
                db, order, "awaiting_confirmation", allowed_from={"shipped"}, commit=False
            )

    db.commit()
    db.refresh(shipment)
    return shipment
