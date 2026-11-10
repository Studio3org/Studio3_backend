"""Orders DAO."""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from src.shared.models.order import Order, OrderItem
from src.shared.models.piece import Piece
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.config import commission as commission_policy
from src.modules.pieces import listing_rules, piece_state

# Orders in these statuses represent a completed sale/purchase — excludes pending_payment
# (not yet paid), failed, cancelled and refunded. awaiting_confirmation counts: the money is
# captured and the artwork has been delivered, only the collector's confirmation is pending.
COMPLETED_STATUSES = ("paid", "shipped", "awaiting_confirmation", "completed")

# Orders still "in flight" from the seller's perspective — not yet fully completed or
# cancelled. Used to block seller deactivation until these are resolved. `disputed` and
# `awaiting_confirmation` belong here: both can still end in a payout or a refund, so a
# seller must not be able to walk away mid-flight.
IN_PROGRESS_STATUSES = (
    "pending_payment",
    "paid",
    "shipped",
    "awaiting_confirmation",
    "disputed",
)

# The single source of truth for order status changes. Every module (webhooks, shipments,
# delivery confirmation, admin dispute resolution) goes through transition_order() below
# rather than assigning order.status directly.
VALID_TRANSITIONS = {
    "pending_payment": {"paid", "failed", "cancelled"},
    "paid": {"shipped", "cancelled", "refunded", "disputed"},
    # Refundable in transit and on arrival (lost or damaged in shipping). These two edges were
    # missing, so an admin refund of a shipped order sent the money back at Stripe and then
    # failed to record it: the order stayed shipped, the ledger unbooked, the payout live.
    "shipped": {"awaiting_confirmation", "disputed", "refunded"},
    "awaiting_confirmation": {"completed", "disputed", "refunded"},
    "disputed": {"completed", "refunded"},
    # A delivered order can still be refunded — a dashboard-issued refund or a late dispute
    # both land here. Without this edge charge.refunded raised 409 inside the webhook, which
    # became a 500 and a retry loop.
    "completed": {"refunded", "disputed"},
}

# Statuses from which no further transition is possible. Descriptive only — the authority
# is VALID_TRANSITIONS above, which is what transition_order actually enforces. `completed`
# is deliberately absent: a delivered order can still be refunded or disputed.
TERMINAL_STATUSES = tuple(
    status for status in ("cancelled", "failed", "refunded") if not VALID_TRANSITIONS.get(status)
)


def count_seller_in_progress(db: Session, seller_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count(Order.id)).where(
            Order.seller_id == seller_id, Order.status.in_(IN_PROGRESS_STATUSES)
        )
    ).scalar_one()


def count_buyer_in_progress(db: Session, buyer_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count(Order.id)).where(
            Order.buyer_id == buyer_id, Order.status.in_(IN_PROGRESS_STATUSES)
        )
    ).scalar_one()


def create_order(
    db: Session,
    buyer_id: uuid.UUID,
    seller_id: uuid.UUID,
    piece: Piece,
    shipping_method: str,
    address_snapshot: dict,
    artwork_cents: int,
    shipping_cents: int,
    tax_cents: int,
    total_cents: int,
) -> Order:
    # Re-read the piece under a row lock and re-check availability. The caller already
    # checked, but without the lock two concurrent checkouts can both pass that check before
    # either commits and end up double-reserving one physical artwork.
    locked = db.execute(
        select(Piece).where(Piece.id == piece.id).with_for_update()
    ).scalar_one_or_none()
    if not locked:
        raise AppError("This piece is no longer available.", 409)
    # Checked here and not only in the controller, for the same reason the lock is here: the
    # DAO is the authority. The listing_type half of this was missing entirely, so an auction
    # piece — is_for_sale, live, price_cents holding the STARTING BID — satisfied every check
    # and could be bought outright mid-auction for the opening price.
    listing_rules.assert_purchasable_fixed(locked)

    order = Order(
        id=uuid.uuid4(),
        buyer_id=buyer_id,
        seller_id=seller_id,
        shipping_method=shipping_method,
        shipping_address_snapshot=address_snapshot,
        artwork_cents=artwork_cents,
        shipping_cents=shipping_cents,
        tax_cents=tax_cents,
        total_cents=total_cents,
        # Resolved once, here, and never recomputed. Changing the platform rate later
        # must not restate what this artist is owed.
        commission_bps=commission_policy.resolve_bps(
            db.get(User, seller_id), commission_policy.SALE_ART
        ),
    )
    db.add(order)
    db.flush()
    db.add(OrderItem(id=uuid.uuid4(), order_id=order.id, piece_id=locked.id, price_cents=artwork_cents))
    piece_state.transition_piece(
        db, locked, piece_state.RESERVED, allowed_from={piece_state.LIVE}, commit=False
    )
    db.commit()
    db.refresh(order)
    return order


def create_auction_order(
    db: Session,
    buyer_id: uuid.UUID,
    seller_id: uuid.UUID,
    piece: Piece,
    winning_bid_id: uuid.UUID,
    shipping_method: str,
    address_snapshot: dict,
    artwork_cents: int,
    shipping_cents: int,
    tax_cents: int,
    total_cents: int,
    prepaid_cents: int = 0,
    prepaid_fee_cents: int = 0,
    prepaid_reference: Optional[str] = None,
) -> Order:
    """Same hand-off as create_order, but for an auction winner completing checkout after
    the auction has already closed (piece.status == "auction_won") rather than a live,
    still-purchasable piece — everything downstream (payment intent, webhook, shipment,
    delivery confirmation) is unchanged from this point on.

    One thing is not the same, and it is the reason `prepaid_cents` exists: closing the
    auction captured the winner's hold, so the hammer price has **already been taken**. The
    payment intent for this order is raised on total minus prepaid. Before this was tracked,
    checkout priced the intent at the full total and charged the artwork a second time — on
    a $5,000 piece, $5,000 taken twice."""
    from src.shared.models.bid import Bid

    locked = db.execute(
        select(Piece).where(Piece.id == piece.id).with_for_update()
    ).scalar_one_or_none()
    if not locked or locked.status != "auction_won":
        raise AppError("This auction hasn't ended yet or has already been claimed.", 409)

    winning_bid = db.get(Bid, winning_bid_id)
    if not winning_bid or winning_bid.bidder_id != buyer_id:
        raise AppError("Only the winning bidder can complete this purchase.", 403)

    order = Order(
        id=uuid.uuid4(),
        buyer_id=buyer_id,
        seller_id=seller_id,
        shipping_method=shipping_method,
        shipping_address_snapshot=address_snapshot,
        artwork_cents=artwork_cents,
        shipping_cents=shipping_cents,
        tax_cents=tax_cents,
        total_cents=total_cents,
        prepaid_cents=prepaid_cents,
        prepaid_fee_cents=prepaid_fee_cents,
        prepaid_reference=prepaid_reference,
        # Resolved once, here, and never recomputed. Changing the platform rate later
        # must not restate what this artist is owed.
        commission_bps=commission_policy.resolve_bps(
            db.get(User, seller_id), commission_policy.SALE_ART
        ),
    )
    db.add(order)
    db.flush()
    db.add(OrderItem(id=uuid.uuid4(), order_id=order.id, piece_id=locked.id, price_cents=artwork_cents))
    piece_state.transition_piece(
        db, locked, piece_state.RESERVED, allowed_from={piece_state.AUCTION_WON}, commit=False
    )
    db.commit()
    db.refresh(order)
    return order


def get_order(db: Session, order_id: uuid.UUID) -> Optional[Order]:
    return db.get(Order, order_id)


def get_order_for_update(db: Session, order_id: uuid.UUID) -> Optional[Order]:
    """Row-locked fetch. Use on any read-modify-write path (webhook handlers, payout
    release, dispute resolution) so a concurrent webhook and user action serialize instead
    of racing on the same order."""
    return db.execute(
        select(Order).where(Order.id == order_id).with_for_update()
    ).scalar_one_or_none()


def get_order_by_payment_reference(db: Session, payment_intent_id: str) -> Optional[Order]:
    return db.execute(
        select(Order).where(Order.payment_reference == payment_intent_id)
    ).scalar_one_or_none()


def transition_order(
    db: Session,
    order: Order,
    new_status: str,
    allowed_from: Optional[set] = None,
    commit: bool = True,
) -> Order:
    """Move an order to `new_status`, enforcing VALID_TRANSITIONS.

    The one place order.status is written. Callers that need the change to be atomic with
    other writes (e.g. booking a ledger transaction in the same DB transaction) pass
    commit=False and commit themselves.

    `allowed_from` narrows the permitted source states further for a specific caller — e.g.
    the refund path only allows disputed/paid, never completed.
    """
    current = order.status
    if current == new_status:
        return order  # idempotent: a duplicate webhook delivery is a no-op, not an error
    if allowed_from is not None and current not in allowed_from:
        raise AppError(f"Cannot transition order from '{current}' to '{new_status}'.", 409)
    if new_status not in VALID_TRANSITIONS.get(current, set()):
        raise AppError(f"Cannot transition order from '{current}' to '{new_status}'.", 409)
    order.status = new_status
    if commit:
        db.commit()
        db.refresh(order)
    return order


def release_pieces(db: Session, order: Order, commit: bool = True) -> None:
    """Put an order's pieces back on the market — payment failed, or the order was
    cancelled or refunded, so the artwork must not stay reserved forever.

    An auction does not return to `live`: its auction_ends_at is already in the past, so the
    close sweep would immediately re-close it. release_target_status sends it back to its
    winner instead, or delists it if the winning bid is gone.
    """
    for item in list_items(db, order.id):
        piece = db.get(Piece, item.piece_id)
        if piece and piece.status in (piece_state.RESERVED, piece_state.SOLD):
            piece_state.transition_piece(
                db,
                piece,
                piece_state.release_target_status(db, piece),
                allowed_from={piece_state.RESERVED, piece_state.SOLD},
                reason="order_released",
                commit=False,
            )
    if commit:
        db.commit()


def list_items(db: Session, order_id: uuid.UUID) -> list[OrderItem]:
    return list(db.execute(select(OrderItem).where(OrderItem.order_id == order_id)).scalars().all())


def list_buyer_orders(
    db: Session, buyer_id: uuid.UUID, limit: int = 20, before: Optional[datetime] = None
) -> list[Order]:
    q = select(Order).where(Order.buyer_id == buyer_id)
    if before:
        q = q.where(Order.created_at < before)
    return list(db.execute(q.order_by(Order.created_at.desc()).limit(limit)).scalars().all())


def list_seller_orders(
    db: Session, seller_id: uuid.UUID, limit: int = 20, before: Optional[datetime] = None
) -> list[Order]:
    q = select(Order).where(Order.seller_id == seller_id)
    if before:
        q = q.where(Order.created_at < before)
    return list(db.execute(q.order_by(Order.created_at.desc()).limit(limit)).scalars().all())


def count_buyer_collected(db: Session, buyer_id: uuid.UUID) -> int:
    """Number of pieces a buyer has successfully purchased (paid/shipped/completed orders)."""
    return db.execute(
        select(func.count(Order.id)).where(
            Order.buyer_id == buyer_id, Order.status.in_(COMPLETED_STATUSES)
        )
    ).scalar_one()


def count_seller_sales(db: Session, seller_id: uuid.UUID) -> int:
    """Number of completed sales for a seller (paid/shipped/completed orders)."""
    return db.execute(
        select(func.count(Order.id)).where(
            Order.seller_id == seller_id, Order.status.in_(COMPLETED_STATUSES)
        )
    ).scalar_one()


def piece_summaries(db: Session, piece_ids) -> dict:
    """{piece_id: {id, title, mediaUrl, listingType}} for an order list, in two queries at most.

    B34: order rows name and show their piece without the app fetching each one. The picture
    follows the rule everywhere else uses (notifications_dao.piece_cover_url): the cover, or
    for a video cover the piece's first photo — batched here instead of a query per piece.
    """
    from src.shared.models.piece_media import PieceMedia

    ids = {pid for pid in piece_ids if pid}
    if not ids:
        return {}
    pieces = db.execute(select(Piece).where(Piece.id.in_(ids))).scalars().all()
    video_ids = [p.id for p in pieces if (p.media_type or "image") == "video"]
    first_photo: dict = {}
    if video_ids:
        rows = db.execute(
            select(PieceMedia.piece_id, PieceMedia.media_url)
            .where(PieceMedia.piece_id.in_(video_ids), PieceMedia.media_type == "image")
            .order_by(PieceMedia.piece_id, PieceMedia.sort_order)
        ).all()
        for piece_id, url in rows:
            first_photo.setdefault(piece_id, url)
    return {
        p.id: {
            "id": str(p.id),
            "title": p.title,
            "mediaUrl": first_photo.get(p.id) if (p.media_type or "image") == "video" else p.media_url,
            "listingType": p.listing_type,
        }
        for p in pieces
    }


def order_items_by_order(db: Session, order_ids) -> dict:
    """{order_id: [OrderItem, ...]} for a page of orders in one query."""
    grouped: dict = {oid: [] for oid in order_ids}
    if not grouped:
        return grouped
    for item in db.execute(
        select(OrderItem).where(OrderItem.order_id.in_(list(grouped)))
    ).scalars():
        grouped[item.order_id].append(item)
    return grouped


def orders_to_dicts(db: Session, orders: list[Order]) -> list[dict]:
    """order_to_dict for a page, with its items and pieces fetched once for the whole page."""
    items = order_items_by_order(db, [o.id for o in orders])
    pieces = piece_summaries(db, [i.piece_id for rows in items.values() for i in rows])
    return [order_to_dict(db, o, items=items[o.id], pieces=pieces) for o in orders]


def order_to_dict(
    db: Session, order: Order, *, items: list | None = None, pieces: dict | None = None
) -> dict:
    """Order payload for buyer/seller clients.

    Shipment and payout state are embedded rather than exposed as separate endpoints, so an
    order-detail screen needs one request. Payout is summarised to a status only — the
    Stripe transfer id and failure details are ops information, not something to hand to
    either party.
    """
    from src.shared.models.dispute import Dispute
    from src.shared.models.payout import Payout
    from src.shared.models.shipment import Shipment

    if items is None:
        items = list_items(db, order.id)
    if pieces is None:
        pieces = piece_summaries(db, [i.piece_id for i in items])
    shipment = db.execute(
        select(Shipment).where(Shipment.order_id == order.id)
    ).scalar_one_or_none()
    payout = db.execute(select(Payout).where(Payout.order_id == order.id)).scalar_one_or_none()
    dispute = db.execute(select(Dispute).where(Dispute.order_id == order.id)).scalar_one_or_none()

    return {
        "id": str(order.id),
        "buyerId": str(order.buyer_id),
        "sellerId": str(order.seller_id),
        "status": order.status,
        "shippingMethod": order.shipping_method,
        "shippingAddress": order.shipping_address_snapshot,
        "artworkCents": order.artwork_cents,
        "shippingCents": order.shipping_cents,
        "taxCents": order.tax_cents,
        "totalCents": order.total_cents,
        "paymentProvider": order.payment_provider,
        "received": order.received,
        "receivedAt": order.received_at.isoformat() if order.received_at else None,
        "items": [
            {
                "pieceId": str(i.piece_id),
                "priceCents": i.price_cents,
                "quantity": i.quantity,
                # B34: the piece, so a Sales or Purchases row can show it without a fetch.
                # Null if the piece no longer exists.
                "piece": pieces.get(i.piece_id),
            }
            for i in items
        ],
        # True for an auction win (the hammer price was collected before checkout), so a
        # row can say AUCTION rather than SOLD (B34).
        "fromAuction": (order.prepaid_cents or 0) > 0,
        "shipment": shipment_view(shipment),
        # Phase 8: insurance is part of shippingCents; the delivery type chosen at checkout.
        "insuranceCents": order.insurance_cents or 0,
        "deliveryType": order.delivery_type,
        "payoutStatus": payout.status if payout else None,
        "dispute": {
            "status": dispute.status,
            "reason": dispute.reason,
            "openedAt": dispute.created_at.isoformat(),
        } if dispute else None,
        "createdAt": order.created_at.isoformat(),
        "updatedAt": order.updated_at.isoformat(),
    }



# The tracker the app draws (B32), in order. A Convelio status maps onto the step it's in.
TRACKER_STEPS = ("booked", "picked_up", "in_transit", "out_for_delivery", "delivered")
_STEP_OF = {
    "shipment_created": "booked", "picked_up": "picked_up", "packing_in_progress": "in_transit",
    "export_in_progress": "in_transit", "freight_in_transit": "in_transit",
    "import_in_progress": "in_transit", "out_for_delivery": "out_for_delivery",
    "shipment_completed": "delivered",
}
_STEP_OF_MANUAL = {"label_created": "booked", "picked_up": "picked_up", "in_transit": "in_transit",
                   "delivered": "delivered"}


def shipment_view(shipment) -> Optional[dict]:
    """The order's shipment: the old fields, plus Convelio's (Phase 8, B32)."""
    if shipment is None:
        return None
    step = _STEP_OF.get(shipment.provider_status or "") or _STEP_OF_MANUAL.get(shipment.status, "booked")
    reached = TRACKER_STEPS.index(step)
    return {
        "courier": shipment.courier or ("Convelio" if shipment.provider == "convelio" else None),
        "trackingNumber": shipment.tracking_number or shipment.provider_order_ref,
        "status": shipment.status,
        "shipmentDate": shipment.shipment_date.isoformat() if shipment.shipment_date else None,
        "provider": shipment.provider,
        "providerStatus": shipment.provider_status,
        "trackingUrl": shipment.tracking_link,
        "reference": shipment.provider_order_ref,
        "deliveredAt": shipment.delivered_at.isoformat() if shipment.delivered_at else None,
        "onHold": shipment.provider_status == "on_hold",
        "cancelled": shipment.provider_status == "canceled",
        "steps": [{"key": key, "done": i <= reached} for i, key in enumerate(TRACKER_STEPS)],
    }
