"""Auction actions a person takes: the seller's, and the winner's.

Everything here is a thin authorization-and-validation shell over a primitive that already
exists. The rules that matter — capture before release, keep the runners-up funded while a
cascade is possible, void bids before moving the piece — live in auction_winner,
auction_cancellation and auction_dao, so that a rule cannot be enforced in the endpoint and
forgotten in the sweep that does the same thing.
"""
import uuid
from datetime import datetime, timezone

from flask import g, request

from src.shared.config.database import SessionLocal
from src.shared.models.auction import (
    AUCTION_AWAITING_PAYMENT,
    AUCTION_DRAFT,
    AUCTION_CLOSED_NO_BIDS,
    AUCTION_CLOSED_RESERVE_NOT_MET,
    AUCTION_NEEDS_SELLER_ACTION,
    Auction,
)
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.bids import (
    auction_cancellation,
    auction_dao,
    auction_winner,
    bid_dao,
)
from src.modules.notifications import notifications_dao
from src.modules.pieces import listing_rules, piece_state
from src.modules.pieces.pieces_dao import get_piece
from src.modules.user.user_dao import get_user_by_id

logger = get_logger(__name__)


def _owned_piece(db, piece_id: str, user_id: uuid.UUID):
    piece = get_piece(db, uuid.UUID(piece_id))
    if not piece:
        raise AppError("Piece not found.", 404)
    if piece.user_id != user_id:
        # 404, not 403: whether a piece you do not own has a running auction is not yours
        # to learn from an error code.
        raise AppError("Piece not found.", 404)
    return piece


def bid_history(piece_id: str):
    """Every bid on the artist's own auction, with anonymous bidder numbers (B38)."""
    from src.modules.bids import seller_view

    db = SessionLocal()
    try:
        piece = _owned_piece(db, piece_id, uuid.UUID(g.user["id"]))
        return seller_view.bid_history(db, piece.id), 200
    finally:
        db.close()


def extend(piece_id: str):
    """The one manual extension a seller gets on a standalone auction."""
    body = request.get_json() or {}
    try:
        extra_days = int(body.get("extraDays"))
    except (TypeError, ValueError):
        raise AppError("extraDays is required.", 400) from None

    db = SessionLocal()
    try:
        seller_id = uuid.UUID(g.user["id"])
        piece = _owned_piece(db, piece_id, seller_id)
        auction = auction_dao.get_running_auction(db, piece.id, lock=True)
        if auction is None:
            raise AppError("This piece has no auction running.", 409)

        auction_dao.extend_auction(db, auction, extra_days, commit=True)
        _notify_bidders(
            db, auction, piece,
            type="auction_extended",
            title="This auction now runs longer",
            body=f'The seller extended "{piece.title}" by {extra_days} day'
                 f'{"s" if extra_days != 1 else ""}. Your bid and your hold still stand.',
        )
        return bid_dao.bid_summary(db, auction, viewer_id=seller_id), 200
    finally:
        db.close()


def cancel(piece_id: str):
    """Withdraw a live auction. Every bidder is refunded and told.

    Routed through the shared cancellation primitive rather than reimplemented, because the
    bidders' money is the thing most easily forgotten: the seller-deactivation path once
    delisted live auctions without releasing a single hold.
    """
    db = SessionLocal()
    try:
        seller_id = uuid.UUID(g.user["id"])
        piece = _owned_piece(db, piece_id, seller_id)
        auction = auction_dao.get_running_auction(db, piece.id)
        if auction is None:
            raise AppError("This piece has no auction running.", 409)

        if auction.status == AUCTION_DRAFT:
            # Not open yet: nothing to refund, only the auction itself to call off.
            auction_cancellation.cancel_draft_auction(
                db, piece, auction.id,
                auction_cancellation.REASON_SELLER_CANCELLED,
                actor_id=seller_id,
                target_status=piece_state.DELISTED,
            )
            db.refresh(piece)
            return {
                "pieceId": str(piece.id),
                "auctionId": str(auction.id),
                "cancelledBids": 0,
                "status": piece.status,
            }, 200

        bids = auction_cancellation.cancel_auction(
            db, piece,
            auction_cancellation.REASON_SELLER_CANCELLED,
            actor_id=seller_id,
            target_status=piece_state.DELISTED,
            commit=True,
        )
        return {
            "pieceId": str(piece.id),
            "auctionId": str(auction.id),
            "cancelledBids": len(bids),
            "status": piece.status,
        }, 200
    finally:
        db.close()


RELISTABLE_AUCTION_STATUSES = (
    AUCTION_NEEDS_SELLER_ACTION,     # bids came in under the reserve
    AUCTION_CLOSED_NO_BIDS,          # nobody bid at all
    AUCTION_CLOSED_RESERVE_NOT_MET,  # closed under reserve without parking for a decision
)


def relist(piece_id: str):
    """Run a fresh auction on a piece whose last one ended without a sale.

    A new Auction row, never a revived one. The old row is the record of what happened —
    who bid, what the reserve was, why it ended — and reopening it would overwrite that
    history with the second attempt's.
    """
    body = request.get_json() or {}
    db = SessionLocal()
    try:
        seller_id = uuid.UUID(g.user["id"])
        seller = get_user_by_id(db, seller_id)
        piece = _owned_piece(db, piece_id, seller_id)

        previous = db.execute(
            Auction.__table__.select()
            .where(Auction.piece_id == piece.id)
            .order_by(Auction.created_at.desc())
            .limit(1)
        ).mappings().first()
        # Every ending where nobody was charged and nobody is owed anything. This used to
        # accept needs_seller_action alone, which meant the commonest failure — an auction
        # that simply got no bids — left the work delisted with nothing offering to try
        # again. closed_sold and cancelled stay out: one has a buyer behind it, the other
        # was somebody deciding to stop.
        if not previous or previous["status"] not in RELISTABLE_AUCTION_STATUSES:
            raise AppError(
                "This piece has no unsold auction to relist." if previous
                else "This piece has never been auctioned.",
                409,
            )
        if auction_dao.get_running_auction(db, piece.id) is not None:
            raise AppError("This piece already has an auction running.", 409)

        starting_bid_cents = body.get("startingBidCents") or previous["starting_bid_cents"]
        reserve_cents = body.get("reserveCents", previous["reserve_cents"])
        duration_days = body.get("durationDays") or previous["duration_days"]
        try:
            starting_bid_cents = int(starting_bid_cents)
            duration_days = int(duration_days)
            reserve_cents = int(reserve_cents) if reserve_cents is not None else None
        except (TypeError, ValueError):
            raise AppError("Auction terms must be whole numbers.", 400) from None

        # The same validation a first listing goes through, so a relist cannot reach a shape
        # the create path would have refused.
        listing_rules.validate_listing(
            is_for_sale=True,
            listing_type="auction",
            price_cents=starting_bid_cents,
            auction_duration_days=duration_days,
            seller_enabled=bool(seller.seller_enabled),
            seller=seller,
            reserve_cents=reserve_cents,
        )

        auction = auction_dao.create_auction(
            db, piece, seller,
            starting_bid_cents=starting_bid_cents,
            duration_days=duration_days,
            reserve_cents=reserve_cents,
            commit=False,
        )
        piece_state.transition_piece(
            db, piece, piece_state.LIVE,
            allowed_from={piece_state.DELISTED, piece_state.AUCTION_WON},
            reason="auction_relisted", commit=False,
        )
        auction_dao.open_auction(db, auction, commit=False)
        db.commit()
        db.refresh(auction)
        result = bid_dao.bid_summary(db, auction, viewer_id=seller_id)
        # Everyone who saved it hears it is up for auction again (B12).
        from src.modules.notifications import watchers

        watchers.auction_opened(db, piece, auction)
        return result, 201
    finally:
        db.close()


def retry_winner_payment(piece_id: str):
    """The winner fixes a declined payment inside their window.

    Deliberately available to the winner rather than only to the sweep: at an event the
    window is ten minutes, and waiting for a scheduled task to notice is not a plan when
    there is a person standing in the room with another card in their hand.
    """
    body = request.get_json() or {}
    payment_method_id = (body.get("paymentMethodId") or "").strip()
    if not payment_method_id:
        raise AppError("paymentMethodId is required.", 400)

    db = SessionLocal()
    try:
        user_id = uuid.UUID(g.user["id"])
        bidder = get_user_by_id(db, user_id)
        piece = get_piece(db, uuid.UUID(piece_id))
        if not piece:
            raise AppError("Piece not found.", 404)

        auction = auction_dao.get_settling_auction(db, piece.id, lock=True)
        if auction is None or auction.status != AUCTION_AWAITING_PAYMENT:
            raise AppError("There's no payment waiting to be fixed on this piece.", 409)

        winning = auction_winner.winning_bid(db, auction)
        if not winning or winning.bidder_id != user_id:
            raise AppError("Only the winning bidder can do this.", 403)

        # Phase 7a: the new card is checked and charged straight away. The fix window does not
        # move however many cards are tried (BE-4).
        from src.modules.bids import auction_payments

        payment_method_id = auction_payments.check_card(db, bidder, payment_method_id)
        outcome = auction_winner.settle_on(db, auction, piece, winning, commit=False,
                                           payment_method_id=payment_method_id)
        db.commit()

        if outcome != auction_winner.SETTLED:
            raise AppError(
                "That card was declined too. Try another one before your time runs out.", 402
            )

        _notify_payment_fixed(db, auction, piece, winning)
        return bid_dao.bid_summary(db, auction, viewer_id=user_id), 200
    finally:
        db.close()


def _notify_payment_fixed(db, auction, piece, winning) -> None:
    title = piece.title if piece else "a piece"
    for user_id, kind, heading, body in (
        (winning.bidder_id, "auction_payment_fixed", "Payment received",
         f'Your payment for "{title}" went through. Confirm your delivery details to finish.'),
        (auction.seller_id, "auction_sold", "Your auction sold",
         f'"{title}" sold for ${winning.amount_cents / 100:.2f}.'),
    ):
        try:
            notifications_dao.create_and_push(
                db, user_id=user_id, type=kind, target_type="piece",
                target_id=auction.piece_id,
                payload={"amountCents": winning.amount_cents},
                title=heading, body=body,
            )
        except Exception:
            logger.exception("Payment-fixed notification failed for user %s", user_id)


def _notify_bidders(db, auction, piece, *, type: str, title: str, body: str) -> None:
    """One notification per distinct active bidder. Never raises."""
    seen = set()
    for bid in bid_dao.ranked_active_bids(db, auction.id):
        if bid.bidder_id in seen:
            continue
        seen.add(bid.bidder_id)
        try:
            notifications_dao.create_and_push(
                db, user_id=bid.bidder_id, type=type, target_type="piece",
                target_id=piece.id, title=title, body=body,
            )
        except Exception:
            logger.exception("Notification %s failed for bidder %s", type, bid.bidder_id)



# --- Phase 7a: second chance --------------------------------------------------------------------

def respond_to_offer(piece_id: str, accept: bool):
    """The next bidder's answer to a second-chance offer. Accepting charges their saved card
    (or `paymentMethodId`) at once; declining passes it to the next bidder."""
    from src.modules.bids import auction_closer, auction_payments

    body = request.get_json(silent=True) or {}
    db = SessionLocal()
    try:
        user_id = uuid.UUID(g.user["id"])
        bidder = get_user_by_id(db, user_id)
        piece = get_piece(db, uuid.UUID(piece_id))
        if not piece:
            raise AppError("Piece not found.", 404)
        auction = auction_dao.get_settling_auction(db, piece.id, lock=True)
        offer = auction_payments.pending_offer(db, auction.id, lock=True) if auction else None
        if offer is None or offer.bidder_id != user_id:
            raise AppError("There's no offer waiting for you on this piece.", 404)
        if offer.expires_at <= datetime.now(timezone.utc):
            raise AppError("This offer has expired.", 409)
        if not accept:
            outcome = auction_winner.pass_offer(db, auction, piece, offer, expired=False)
            db.commit()
            auction_closer.after_offer(db, auction, piece, outcome)
            return {"declined": True}, 200
        method = (body.get("paymentMethodId") or "").strip() or None
        if method:
            method = auction_payments.check_card(db, bidder, method)
        outcome = auction_winner.accept_offer(db, auction, piece, offer, method)
        db.commit()
        if outcome != auction_winner.SETTLED:
            raise AppError(f"{offer.last_error or 'The card was declined.'} Try another card "
                           "before the offer runs out.", 402)
        winning = auction_winner.winning_bid(db, auction)
        auction_closer._notify_close(db, auction, piece, winning, [])
        return bid_dao.bid_summary(db, auction, viewer_id=user_id), 200
    finally:
        db.close()
