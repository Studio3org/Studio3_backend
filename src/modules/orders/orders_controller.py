"""Orders controller — checkout, delivery confirmation, and dispute reporting.

Payment capture itself is webhook-driven (src/modules/payments); nothing here marks an
order paid when Stripe is configured.
"""
import os
import uuid
from datetime import datetime, timezone

from flask import request, g
from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.models.dispute import Dispute, DISPUTE_OPEN
from src.shared.models.payout import Payout, PAYOUT_PENDING
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.user.user_dao import get_user_by_id
from src.modules.pieces.pieces_dao import get_piece
from src.modules.addresses import addresses_dao
from src.modules.orders import orders_dao
from src.modules.pieces import listing_rules, piece_state
from src.modules.bids import auction_dao, bid_dao
from src.modules.orders.shipping import SHIPPING_RATES_CENTS, FLAT_TAX_RATE
from src.modules.notifications import notifications_dao
from src.modules.payments import money

logger = get_logger(__name__)

# Transitions a buyer or seller may drive directly through PATCH /orders/:id. Everything
# else — paid, failed, refunded, awaiting_confirmation, completed, disputed — is
# money-affecting and is driven only by webhooks or internal service calls (NFR-1), going
# through orders_dao.transition_order().
CLIENT_DRIVEN_TRANSITIONS = {"cancelled"}


def get_shipping_quote(piece_id: str):
    db = SessionLocal()
    try:
        piece = get_piece(db, uuid.UUID(piece_id))
        if not piece:
            raise AppError("Piece not found.", 404)
        methods = [{"id": method, "cents": cents} for method, cents in SHIPPING_RATES_CENTS.items()]
        return {"methods": methods}, 200
    finally:
        db.close()


def collect(piece_id: str):
    body = request.get_json() or {}
    address_id = body.get("addressId")
    shipping_method = (body.get("shippingMethod") or "").strip().lower()
    if not address_id:
        raise AppError("addressId is required.", 400)
    if shipping_method not in SHIPPING_RATES_CENTS:
        raise AppError(f"shippingMethod must be one of: {', '.join(SHIPPING_RATES_CENTS)}", 400)
    db = SessionLocal()
    try:
        buyer = get_user_by_id(db, uuid.UUID(g.user["id"]))
        piece = get_piece(db, uuid.UUID(piece_id))
        if not piece:
            raise AppError("Piece not found.", 404)
        if piece.user_id == buyer.id:
            raise AppError("Cannot purchase your own piece.", 400)
        # Checked here as well as inside create_order, which is authoritative under the row
        # lock. Doing it up front means an auction gets the right error ("place a bid
        # instead") rather than whatever the next unrelated check happens to fail on — the
        # address lookup below would otherwise report "Address not found" for a piece that
        # was never purchasable in the first place.
        listing_rules.assert_purchasable_fixed(piece)
        if not piece.price_cents:
            raise AppError("This piece has no price set.", 400)

        address = addresses_dao.get_address(db, uuid.UUID(address_id))
        if not address or address.user_id != buyer.id:
            raise AppError("Address not found.", 404)

        artwork_cents = piece.price_cents
        shipping_cents = SHIPPING_RATES_CENTS[shipping_method]
        tax_cents = round(artwork_cents * FLAT_TAX_RATE)
        total_cents = artwork_cents + shipping_cents + tax_cents

        order = orders_dao.create_order(
            db,
            buyer_id=buyer.id,
            seller_id=piece.user_id,
            piece=piece,
            shipping_method=shipping_method,
            address_snapshot=addresses_dao.address_snapshot(address),
            artwork_cents=artwork_cents,
            shipping_cents=shipping_cents,
            tax_cents=tax_cents,
            total_cents=total_cents,
        )
        # The client secret comes from POST /orders/<id>/create-payment-intent, not from
        # here. This response used to carry a permanently null clientSecret with a comment
        # claiming payments were unbuilt, which read as though checkout was a stub.
        return orders_dao.order_to_dict(db, order), 201
    finally:
        db.close()


def auction_checkout(piece_id: str):
    """Winning bidder completes checkout after auction_closer flipped the piece to
    auction_won. Priced from the winning Bid, not piece.price_cents (that's only the
    starting bid) — otherwise identical to collect()."""
    body = request.get_json() or {}
    address_id = body.get("addressId")
    shipping_method = (body.get("shippingMethod") or "").strip().lower()
    if not address_id:
        raise AppError("addressId is required.", 400)
    if shipping_method not in SHIPPING_RATES_CENTS:
        raise AppError(f"shippingMethod must be one of: {', '.join(SHIPPING_RATES_CENTS)}", 400)
    db = SessionLocal()
    try:
        buyer = get_user_by_id(db, uuid.UUID(g.user["id"]))
        piece = get_piece(db, uuid.UUID(piece_id))
        if not piece:
            raise AppError("Piece not found.", 404)
        if piece.status != "auction_won":
            raise AppError("This auction hasn't ended yet or has already been claimed.", 409)

        auction = auction_dao.get_running_auction(db, piece.id)
        if auction is None:
            from src.shared.models.auction import Auction

            auction = db.execute(
                select(Auction)
                .where(Auction.piece_id == piece.id)
                .order_by(Auction.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        highest = bid_dao.get_highest_bid(db, auction.id) if auction else None
        if not highest or highest.bidder_id != buyer.id:
            raise AppError("Only the winning bidder can complete this purchase.", 403)

        address = addresses_dao.get_address(db, uuid.UUID(address_id))
        if not address or address.user_id != buyer.id:
            raise AppError("Address not found.", 404)

        artwork_cents = highest.amount_cents
        shipping_cents = SHIPPING_RATES_CENTS[shipping_method]
        tax_cents = round(artwork_cents * FLAT_TAX_RATE)
        total_cents = artwork_cents + shipping_cents + tax_cents

        order = orders_dao.create_auction_order(
            db,
            buyer_id=buyer.id,
            seller_id=piece.user_id,
            piece=piece,
            winning_bid_id=highest.id,
            shipping_method=shipping_method,
            address_snapshot=addresses_dao.address_snapshot(address),
            artwork_cents=artwork_cents,
            shipping_cents=shipping_cents,
            tax_cents=tax_cents,
            total_cents=total_cents,
        )
        return orders_dao.order_to_dict(db, order), 201
    finally:
        db.close()


def confirm(order_id: str):
    """Legacy checkout confirm.

    With Stripe configured this is a read-only status report — the webhook is what marks an
    order paid, never a client call (NFR-1). Without Stripe configured it keeps the
    original dev-mode auto-succeed so local testing works without keys.
    """
    db = SessionLocal()
    try:
        buyer_id = uuid.UUID(g.user["id"])
        order = orders_dao.get_order(db, uuid.UUID(order_id))
        if not order or order.buyer_id != buyer_id:
            raise AppError("Order not found.", 404)

        if os.getenv("STRIPE_SECRET_KEY"):
            result = orders_dao.order_to_dict(db, order)
            result["paid"] = order.status not in ("pending_payment", "failed", "cancelled")
            return result, 200

        if order.status != "pending_payment":
            raise AppError("This order has already been processed.", 409)

        # Dev mode: no payment provider configured — auto-succeed so the rest of the
        # order lifecycle (inventory lock, history, notifications) is testable today.
        orders_dao.transition_order(db, order, "paid", commit=False)
        items = orders_dao.list_items(db, order.id)
        for item in items:
            piece = get_piece(db, item.piece_id)
            if piece:
                piece_state.transition_piece(
                    db, piece, piece_state.SOLD,
                    allowed_from={piece_state.RESERVED}, commit=False,
                )
        # Dev mode still books the ledger and creates the payout row, so the rest of the
        # escrow flow (release, refund) is exercisable without Stripe keys.
        db.add(
            Payout(
                id=uuid.uuid4(),
                order_id=order.id,
                seller_id=order.seller_id,
                status=PAYOUT_PENDING,
                idempotency_key=f"payout:{order.id}",
            )
        )
        money.book_order_paid(db, order, stripe_fee_cents=0, commit=False)
        db.commit()
        db.refresh(order)

        buyer = get_user_by_id(db, order.buyer_id)
        piece_title = None
        if items:
            piece = get_piece(db, items[0].piece_id)
            piece_title = piece.title if piece else None
        notifications_dao.create_and_push(
            db,
            user_id=order.seller_id,
            type="purchase",
            actor_id=order.buyer_id,
            target_type="order",
            target_id=order.id,
            payload={"pieceTitle": piece_title, "totalCents": order.total_cents},
            title="You made a sale!",
            body=f"{buyer.name} purchased '{piece_title}'" if piece_title else f"{buyer.name} completed a purchase",
        )
        notifications_dao.create_and_push(
            db,
            user_id=order.buyer_id,
            type="purchase",
            actor_id=order.seller_id,
            target_type="order",
            target_id=order.id,
            payload={"pieceTitle": piece_title},
            title="Order confirmed",
            body=f"Your order for '{piece_title}' is confirmed" if piece_title else "Your order is confirmed",
        )
        result = orders_dao.order_to_dict(db, order)
        result["devMode"] = True
        return result, 200
    finally:
        db.close()


def _require_participant(order, user_id):
    if not order or (order.buyer_id != user_id and order.seller_id != user_id):
        raise AppError("Order not found.", 404)


def confirm_received(order_id: str):
    """Collector confirms the artwork arrived — the sole happy-path payout trigger.

    Only the buyer can call this. It is what releases the artist's money, so allowing the
    seller (or an automatic timeout) to do it would defeat holding the funds at all.
    """
    db = SessionLocal()
    try:
        user_id = uuid.UUID(g.user["id"])
        order = orders_dao.get_order_for_update(db, uuid.UUID(order_id))
        if not order or order.buyer_id != user_id:
            raise AppError("Order not found.", 404)
        if order.status == "completed":
            return orders_dao.order_to_dict(db, order), 200  # idempotent double-tap
        if order.status != "awaiting_confirmation":
            raise AppError(
                "This order isn't awaiting confirmation yet.", 409
            )

        order.received = True
        order.received_at = datetime.now(timezone.utc)
        orders_dao.transition_order(db, order, "completed", commit=False)
        db.commit()
        db.refresh(order)
    finally:
        db.close()

    # Payout runs after the status commit, deliberately: it makes an external Stripe call,
    # and a failure there must leave the order completed (the artwork did arrive) with the
    # payout retryable from the admin queue, not roll back the collector's confirmation.
    from src.modules.payments import payouts_service

    payouts_service.release_payout_for_order(uuid.UUID(order_id))

    db = SessionLocal()
    try:
        order = orders_dao.get_order(db, uuid.UUID(order_id))
        return orders_dao.order_to_dict(db, order), 200
    finally:
        db.close()


def report_issue(order_id: str):
    """Collector reports a problem instead of confirming — blocks payout, opens a dispute
    for ops to resolve manually."""
    body = request.get_json() or {}
    reason = (body.get("reason") or "").strip()
    if not reason:
        raise AppError("Tell us what went wrong so we can help.", 400)
    if len(reason) > 2000:
        raise AppError("Please keep the description under 2000 characters.", 400)

    db = SessionLocal()
    try:
        user_id = uuid.UUID(g.user["id"])
        order = orders_dao.get_order_for_update(db, uuid.UUID(order_id))
        if not order or order.buyer_id != user_id:
            raise AppError("Order not found.", 404)
        if order.status == "disputed":
            return orders_dao.order_to_dict(db, order), 200
        if order.status not in ("shipped", "awaiting_confirmation", "paid"):
            raise AppError(f"Can't report an issue on a {order.status} order.", 409)

        existing = db.execute(
            select(Dispute).where(Dispute.order_id == order.id)
        ).scalar_one_or_none()
        if not existing:
            db.add(
                Dispute(
                    id=uuid.uuid4(),
                    order_id=order.id,
                    opened_by_id=user_id,
                    reason=reason,
                    status=DISPUTE_OPEN,
                )
            )
        orders_dao.transition_order(db, order, "disputed", commit=False)
        db.commit()
        db.refresh(order)

        # No scheduler exists in this codebase, so the admin queue is the alert: notify any
        # admin so a disputed order can't sit unseen.
        try:
            for admin in db.execute(select(User).where(User.is_admin.is_(True))).scalars():
                notifications_dao.create_and_push(
                    db,
                    user_id=admin.id,
                    type="system",
                    actor_id=user_id,
                    target_type="order",
                    target_id=order.id,
                    payload={"reason": reason},
                    title="Order disputed",
                    body=f"A collector reported an issue on order {str(order.id)[:8]}",
                )
        except Exception as e:
            logger.warning("Admin dispute notification failed for %s: %s", order.id, e)

        return orders_dao.order_to_dict(db, order), 200
    finally:
        db.close()


def get_detail(order_id: str):
    db = SessionLocal()
    try:
        user_id = uuid.UUID(g.user["id"])
        order = orders_dao.get_order(db, uuid.UUID(order_id))
        _require_participant(order, user_id)
        return orders_dao.order_to_dict(db, order), 200
    finally:
        db.close()


def patch(order_id: str):
    body = request.get_json() or {}
    new_status = (body.get("status") or "").strip().lower()
    if not new_status:
        raise AppError("status is required.", 400)
    db = SessionLocal()
    try:
        user_id = uuid.UUID(g.user["id"])
        # Locked: cancellation races a payment webhook that may be flipping the same order
        # to paid right now.
        order = orders_dao.get_order_for_update(db, uuid.UUID(order_id))
        _require_participant(order, user_id)

        if new_status not in CLIENT_DRIVEN_TRANSITIONS:
            # Shipment status is now ops-driven (POST/PATCH /orders/:id/shipment) and all
            # payment-affecting transitions are webhook-driven, so there is nothing else a
            # buyer or seller may set here directly.
            raise AppError(f"Status '{new_status}' cannot be set directly.", 403)
        if order.status not in ("pending_payment", "paid"):
            raise AppError(f"Cannot cancel an order in status '{order.status}'.", 409)

        orders_dao.transition_order(db, order, "cancelled", commit=False)
        orders_dao.release_pieces(db, order, commit=False)
        db.commit()
        db.refresh(order)
        return orders_dao.order_to_dict(db, order), 200
    finally:
        db.close()


def list_my_orders():
    cursor = request.args.get("cursor")
    limit = min(int(request.args.get("limit", 20)), 50)
    db = SessionLocal()
    try:
        user_id = uuid.UUID(g.user["id"])
        before = datetime.fromisoformat(cursor) if cursor else None
        orders = orders_dao.list_buyer_orders(db, user_id, limit=limit + 1, before=before)
        has_more = len(orders) > limit
        orders = orders[:limit]
        items = [orders_dao.order_to_dict(db, o) for o in orders]
        next_cursor = orders[-1].created_at.isoformat() if has_more and orders else None
        return {"items": items, "nextCursor": next_cursor}, 200
    finally:
        db.close()


def list_my_sales():
    cursor = request.args.get("cursor")
    limit = min(int(request.args.get("limit", 20)), 50)
    db = SessionLocal()
    try:
        user_id = uuid.UUID(g.user["id"])
        before = datetime.fromisoformat(cursor) if cursor else None
        orders = orders_dao.list_seller_orders(db, user_id, limit=limit + 1, before=before)
        has_more = len(orders) > limit
        orders = orders[:limit]
        items = [orders_dao.order_to_dict(db, o) for o in orders]
        next_cursor = orders[-1].created_at.isoformat() if has_more and orders else None
        return {"items": items, "nextCursor": next_cursor}, 200
    finally:
        db.close()
