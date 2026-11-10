"""Phase 7b: no checkout after winning (the 9 Nov design, "Winner").

Once the hammer price has been charged (Phase 7a), the rest — Convelio shipping, insurance and
tax — is charged to the same saved card without the winner doing anything:

1. Convelio is asked for a quote to the winner's Ship to address (given at their first bid).
2. **Instant price:** the winner's order is created and the balance charged off-session at
   once. It's paid through the same path as any order (the Stripe webhook's), so the ledger,
   the payout row and the courier booking are exactly a normal sale's.
3. **Custom quote** (most US routes in the sandbox today): the auction waits in
   `pending_quote`; the winner is told the shipping price is on its way, and Convelio's
   `custom_quote_ready` webhook finishes step 2.
4. **The balance charge fails:** the order is called off (the piece goes back to the winner)
   and they finish at the existing auction checkout with another card.

Not used — the winner checks out as before — when the piece is collected at the event,
there's no Ship to address or saved card, Convelio is off, or the artist hasn't added what
Convelio needs.
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.config.stripe_client import get_stripe, platform_currency, stripe_configured
from src.shared.models.address import Address
from src.shared.models.auction import (
    AUCTION_AWAITING_WINNER,
    AUTO_CHARGED,
    AUTO_FAILED,
    AUTO_MANUAL,
    AUTO_PENDING_QUOTE,
    Auction,
)
from src.shared.models.bid import Bid
from src.shared.models.piece import Piece
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)


def queue(db, auction: Auction) -> None:
    """Call inside the transaction that settled the auction on its winner."""
    from src.jobs.dispatch import enqueue_after_commit

    enqueue_after_commit(db, "src.modules.bids.auction_autocheckout.start", str(auction.id))


def _eligible(db, auction: Auction, bid: Optional[Bid]) -> Optional[str]:
    """Why not, or None."""
    from src.modules.shipping import shipping_config

    if not shipping_config.enabled():
        return "convelio_off"
    if auction.is_pickup:
        return "pickup"
    if bid is None or not bid.address_id:
        return "no_address"
    return None


def start(auction_id: str) -> str:
    from src.modules.shipping import quote_service
    from src.modules.shipping.convelio_mapper import ShippingNotReady

    db = SessionLocal()
    try:
        auction = db.execute(select(Auction).where(Auction.id == uuid.UUID(auction_id)).with_for_update()).scalar_one_or_none()
        if auction is None or auction.status != AUCTION_AWAITING_WINNER or auction.auto_checkout_state:
            return "skipped"
        bid = db.get(Bid, auction.winning_bid_id) if auction.winning_bid_id else None
        reason = _eligible(db, auction, bid)
        if reason:
            auction.auto_checkout_state = AUTO_MANUAL
            db.commit()
            return reason
        piece = db.get(Piece, auction.piece_id)
        winner = db.get(User, bid.bidder_id)
        address = db.get(Address, bid.address_id)
        try:
            quote = quote_service.request_quote(db, piece=piece, buyer=winner, address=address,
                                                speed="regular_speed", delivery_type="curbside",
                                                insurance=None)
        except (ShippingNotReady, AppError) as exc:
            logger.info("Auction %s: no automatic checkout (%s).", auction.id, exc.message)
            db.rollback()
            auction = db.get(Auction, uuid.UUID(auction_id))
            auction.auto_checkout_state = AUTO_MANUAL
            db.commit()
            return "manual"
        auction = db.execute(select(Auction).where(Auction.id == uuid.UUID(auction_id)).with_for_update()).scalar_one()
        auction.shipping_quote_id = quote.id
        if quote.status == "processing":
            auction.auto_checkout_state = AUTO_PENDING_QUOTE
            db.commit()
            _tell(db, winner.id, auction, "auction_shipping_pending", "Shipping price on its way",
                  "We're getting your shipping price from the courier. We'll charge it to your "
                  "card and book delivery as soon as it's in.")
            return AUTO_PENDING_QUOTE
        db.commit()
    finally:
        db.close()
    return complete(auction_id)


def on_quote_ready(db, quote) -> None:
    """Convelio priced a custom quote: finish any auction waiting on it."""
    auction = db.execute(
        select(Auction).where(Auction.shipping_quote_id == quote.id,
                              Auction.auto_checkout_state == AUTO_PENDING_QUOTE)
    ).scalar_one_or_none()
    if auction is not None:
        from src.jobs.dispatch import enqueue_after_commit

        enqueue_after_commit(db, "src.modules.bids.auction_autocheckout.complete", str(auction.id))


def complete(auction_id: str) -> str:
    """Create the winner's order on the quote and charge the balance."""
    from src.modules.orders import orders_controller, orders_dao

    db = SessionLocal()
    try:
        auction = db.execute(select(Auction).where(Auction.id == uuid.UUID(auction_id)).with_for_update()).scalar_one_or_none()
        if auction is None or auction.status != AUCTION_AWAITING_WINNER or auction.auto_checkout_state == AUTO_CHARGED:
            return "skipped"
        bid = db.get(Bid, auction.winning_bid_id)
        piece = db.get(Piece, auction.piece_id)
        winner = db.get(User, bid.bidder_id)
        address = db.get(Address, bid.address_id)
        try:
            order = orders_controller.create_winner_order(
                db, piece=piece, buyer=winner, winning=bid, address=address,
                body={"quoteId": str(auction.shipping_quote_id)},
            )
        except AppError as exc:
            # Already claimed through the checkout, or the quote can't be used: the winner
            # finishes by hand.
            db.rollback()
            auction = db.get(Auction, uuid.UUID(auction_id))
            auction.auto_checkout_state = AUTO_MANUAL
            db.commit()
            logger.info("Auction %s: automatic checkout fell back to manual (%s).", auction_id, exc.message)
            return AUTO_MANUAL
        auction = db.get(Auction, uuid.UUID(auction_id))
        auction.checkout_order_id = order.id
        db.commit()
        order_id, balance = order.id, order.total_cents - order.prepaid_cents
        method = bid.payment_method_id
        winner_id = winner.id
    finally:
        db.close()

    paid = _charge_balance(order_id, balance, method)
    db = SessionLocal()
    try:
        auction = db.execute(select(Auction).where(Auction.id == uuid.UUID(auction_id)).with_for_update()).scalar_one()
        if paid:
            auction.auto_checkout_state = AUTO_CHARGED
            db.commit()
            _tell(db, winner_id, auction, "auction_shipping_charged", "Delivery booked",
                  f"We've charged ${balance / 100:,.2f} for shipping, insurance and tax, and booked "
                  "the courier. You'll get tracking updates here.")
            return AUTO_CHARGED
        order = orders_dao.get_order_for_update(db, order_id)
        if order is not None and order.status == "pending_payment":
            orders_dao.transition_order(db, order, "cancelled", commit=False)
            orders_dao.release_pieces(db, order, commit=False)  # back to the winner (PAY-3)
        auction.auto_checkout_state = AUTO_FAILED
        auction.checkout_order_id = None
        db.commit()
        _tell(db, winner_id, auction, "auction_shipping_failed", "Finish your purchase",
              "We couldn't charge shipping and tax to your card. Open the piece to finish with "
              "another card.")
        return AUTO_FAILED
    finally:
        db.close()


def _charge_balance(order_id, balance: int, method: Optional[str]) -> bool:
    """Charge what's left (shipping, insurance, tax) off-session, then mark the order paid the
    way every order is: the webhook's own handler, or the no-Stripe path in development."""
    from src.modules.orders import orders_controller, orders_dao
    from src.modules.payments import webhook_handler

    from src.modules.bids.auction_payments import DEV_DECLINED_CARD

    if not stripe_configured() and method == DEV_DECLINED_CARD:
        return False  # development: Stripe's declining test card declines here too
    if balance <= 0 or not stripe_configured():
        db = SessionLocal()
        try:
            order = orders_dao.get_order_for_update(db, order_id)
            if order.status == "pending_payment":
                orders_controller.settle_paid_without_stripe(db, order)
            return True
        finally:
            db.close()
    if not method:
        return False
    db = SessionLocal()
    try:
        order = orders_dao.get_order(db, order_id)
        from src.modules.bids.holds_service import ensure_customer

        customer = ensure_customer(db, db.get(User, order.buyer_id))
        db.commit()
        try:
            intent = get_stripe().PaymentIntent.create(
                amount=balance, currency=platform_currency(), customer=customer,
                payment_method=method, confirm=True, off_session=True,
                metadata={"order_id": str(order.id), "buyer_id": str(order.buyer_id),
                          "kind": "auction_balance"},
                idempotency_key=f"auction_balance:{order.id}",
            )
        except Exception as exc:  # noqa: BLE001 — a decline is an outcome, not an error
            logger.info("Auction balance for order %s declined: %s", order_id, exc)
            return False
        if intent.get("status") != "succeeded":
            return False
        order.payment_reference = intent["id"]
        db.commit()
        webhook_handler._on_payment_succeeded(db, intent)
        return True
    finally:
        db.close()


def _tell(db, user_id, auction: Auction, type_: str, title: str, body: str) -> None:
    from src.modules.notifications import notifications_dao

    try:
        notifications_dao.create_and_push(db, user_id=user_id, type=type_, target_type="piece",
                                          target_id=auction.piece_id, title=title, body=body,
                                          payload={"auctionId": str(auction.id)})
    except Exception:  # noqa: BLE001
        logger.exception("Auction checkout notification %s failed", type_)
