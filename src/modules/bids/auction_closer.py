"""Closing auctions — the sweep that turns held money into a sale, or gives it all back.

Runs on the Celery worker every minute. Failures here are logged, never raised: there is no
caller to return an error to, and one bad auction must not stop the rest of the batch.

Each auction is re-read and re-validated under a row lock before anything is done to it. The
query that selected it is a hint, not a fact — by the time the sweep gets there a bid may
have landed and pushed the close out, or a seller may have cancelled.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from src.shared.config.database import SessionLocal
from src.shared.models.auction import (
    AUCTION_AWAITING_PAYMENT,
    AUCTION_CLOSED_NO_BIDS,
    AUCTION_CLOSING,
    AUCTION_LIVE,
    AUCTION_NEEDS_SELLER_ACTION,
    Auction,
)
from src.shared.models.bid import BID_LOST, Bid
from src.shared.models.piece import Piece
from src.shared.models.user import User
from src.shared.utils.logger import get_logger
from src.modules.bids import auction_winner, bid_dao
from src.modules.notifications import notifications_dao
from src.modules.pieces import piece_state

logger = get_logger(__name__)


def close_expired_auctions() -> None:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        candidates = db.execute(
            select(Auction.id).where(
                Auction.status.in_((AUCTION_LIVE, AUCTION_CLOSING)),
                Auction.closes_at.is_not(None),
                Auction.closes_at <= now,
            )
        ).scalars().all()

        for auction_id in candidates:
            try:
                _close_one(db, auction_id)
            except Exception:
                db.rollback()
                logger.exception("Failed to close auction %s", auction_id)
    finally:
        db.close()


def _close_one(db, auction_id: uuid.UUID) -> None:
    auction = db.execute(
        select(Auction).where(Auction.id == auction_id).with_for_update()
    ).scalar_one_or_none()
    if not auction or auction.status not in (AUCTION_LIVE, AUCTION_CLOSING):
        return
    # Re-checked under the lock: a bid landing in the soft-close window moves this, and the
    # sweep must not close an auction that has just been extended out from under it.
    if not auction.closes_at or auction.closes_at > datetime.now(timezone.utc):
        return

    piece = db.get(Piece, auction.piece_id)
    seller = db.get(User, auction.seller_id)
    winning = bid_dao.get_highest_bid(db, auction.id)

    reserve_met = (
        auction.reserve_cents is None
        or (winning is not None and winning.amount_cents >= auction.reserve_cents)
    )

    if winning is None:
        _close_without_sale(db, auction, piece, AUCTION_CLOSED_NO_BIDS, "auction_no_bids")
        _notify_seller_no_sale(db, seller, piece, "auction_ended_no_bids", "Auction ended",
                               f'"{piece.title}" ended with no bids.' if piece else "Auction ended.")
        return

    if not reserve_met:
        # The client chose option (c): park it and let the seller decide. Nothing is charged,
        # and every bidder gets their money back.
        _close_without_sale(
            db, auction, piece, AUCTION_NEEDS_SELLER_ACTION, "auction_reserve_not_met"
        )
        _notify_seller_no_sale(
            db, seller, piece, "auction_reserve_not_met", "Reserve not met",
            f'"{piece.title}" ended below your reserve. Nothing was charged — decide what to '
            "do next." if piece else "Your auction ended below its reserve.",
        )
        return

    _close_with_winner(db, auction, piece, winning)


def _close_without_sale(db, auction, piece, status: str, reason: str) -> None:
    """No sale: void every bid, release every hold, and park the auction."""
    cancelled = bid_dao.cancel_active_bids(db, auction.id, reason=reason, commit=False)
    auction.status = status
    auction.closed_at = datetime.now(timezone.utc)
    if piece is not None:
        piece_state.transition_piece(
            db, piece, piece_state.DELISTED, allowed_from={piece_state.LIVE},
            reason=reason, commit=False,
        )
    db.commit()

    # After the commit — a failed push must not undo a close.
    for bid in cancelled:
        try:
            notifications_dao.create_and_push(
                db,
                user_id=bid.bidder_id,
                type="auction_no_sale",
                target_type="piece",
                target_id=auction.piece_id,
                title="Auction ended without a sale",
                body="Nothing was charged to you.",
            )
        except Exception:
            logger.exception("No-sale notification failed for bidder %s", bid.bidder_id)


def _close_with_winner(db, auction, piece, winning) -> None:
    """Hand the auction to auction_winner and tell everyone what happened.

    All the ordering that matters — capture before release, keep the runners-up funded while
    a cascade is still possible — lives in auction_winner.settle_on. This function is the
    part that talks to people.
    """
    outcome = auction_winner.settle_on(db, auction, piece, winning, commit=False)
    db.commit()

    # After the commit, always: a failed push must never undo a close that has already moved
    # real money.
    if outcome == auction_winner.SETTLED:
        losing = _bids_with_status(db, auction.id, BID_LOST)
        _notify_close(db, auction, piece, winning, losing)
    else:
        _notify_payment_failed(db, auction, piece, winning)


def _bids_with_status(db, auction_id, status) -> list:
    return list(
        db.execute(
            select(Bid).where(Bid.auction_id == auction_id, Bid.status == status)
        ).scalars()
    )


def _notify_payment_failed(db, auction, piece, winning) -> None:
    """Both parties, immediately — the client was explicit about this for event auctions,
    and there is no reason a standalone seller should learn about it any later.

    The seller is told because at an event they are standing next to the buyer and can sort
    it out on the spot; leaving them to discover it from a dashboard the next morning is the
    failure mode this rule exists to prevent.
    """
    title = piece.title if piece else "a piece"
    deadline = auction.winner_deadline_at
    when = deadline.strftime("%H:%M UTC on %d %b") if deadline else "shortly"
    try:
        notifications_dao.create_and_push(
            db,
            user_id=winning.bidder_id,
            type="auction_payment_failed",
            target_type="piece",
            target_id=auction.piece_id,
            payload={"amountCents": winning.amount_cents,
                     "deadlineAt": deadline.isoformat() if deadline else None},
            title="Your card was declined",
            body=f'You won "{title}", but the payment didn\'t go through. Add another card '
                 f"by {when} or the piece passes to the next bidder.",
        )
    except Exception:
        logger.exception("Payment-failed notification failed for bidder %s", winning.bidder_id)
    try:
        notifications_dao.create_and_push(
            db,
            user_id=auction.seller_id,
            type="auction_winner_payment_failed",
            actor_id=winning.bidder_id,
            target_type="piece",
            target_id=auction.piece_id,
            payload={"amountCents": winning.amount_cents,
                     "deadlineAt": deadline.isoformat() if deadline else None},
            title="The winning bidder's payment failed",
            body=f'"{title}" sold, but the payment was declined. They have until {when} to '
                 "fix it, then it passes to the next bidder.",
        )
    except Exception:
        logger.exception("Payment-failed notification failed for seller %s", auction.seller_id)


def _notify_close(db, auction, piece, winning, losing) -> None:
    from src.modules.bids import auction_autocheckout

    title = piece.title if piece else "a piece"
    try:
        notifications_dao.create_and_push(
            db,
            user_id=winning.bidder_id,
            type="auction_won",
            target_type="piece",
            target_id=auction.piece_id,
            payload={"amountCents": winning.amount_cents},
            title="You won the auction",
            body=f'You won "{title}" for ${winning.amount_cents / 100:.2f}, and your card has '
                 f"been charged. {auction_autocheckout.next_step(auction)}",
        )
        notifications_dao.create_and_push(
            db,
            user_id=auction.seller_id,
            type="auction_sold",
            actor_id=winning.bidder_id,
            target_type="piece",
            target_id=auction.piece_id,
            payload={"amountCents": winning.amount_cents},
            title="Your auction sold",
            body=f'"{title}" sold for ${winning.amount_cents / 100:.2f}.',
        )
    except Exception:
        logger.exception("Auction-won notifications failed for auction %s", auction.id)

    for bid in losing:
        try:
            notifications_dao.create_and_push(
                db,
                user_id=bid.bidder_id,
                type="auction_lost",
                target_type="piece",
                target_id=auction.piece_id,
                title="You didn't win this auction",
                body=f'"{title}" went to another bidder. Nothing was charged to you — '
                     "see more from this artist.",
            )
        except Exception:
            logger.exception("Auction-lost notification failed for bidder %s", bid.bidder_id)


def _notify_seller_no_sale(db, seller, piece, kind: str, title: str, body: str) -> None:
    if not seller:
        return
    try:
        notifications_dao.create_and_push(
            db, user_id=seller.id, type=kind, target_type="piece",
            target_id=piece.id if piece else None, title=title, body=body,
        )
    except Exception:
        logger.exception("No-sale seller notification failed")


# --- winner windows ---------------------------------------------------------------------

def retry_failed_payments(db, now) -> int:
    """Phase 7a: the automatic retries on a winner's saved card inside the fix window."""
    due = db.execute(
        select(Auction.id).where(
            Auction.status == AUCTION_AWAITING_PAYMENT,
            Auction.next_payment_retry_at.is_not(None),
            Auction.next_payment_retry_at <= now,
            Auction.winner_deadline_at > now,
        )
    ).scalars().all()
    paid = 0
    for auction_id in due:
        try:
            auction = db.execute(select(Auction).where(Auction.id == auction_id).with_for_update()).scalar_one_or_none()
            if not auction or auction.status != AUCTION_AWAITING_PAYMENT:
                db.rollback()
                continue
            piece = db.get(Piece, auction.piece_id)
            winning = auction_winner.winning_bid(db, auction)
            if winning is None:
                db.rollback()
                continue
            outcome = auction_winner.settle_on(db, auction, piece, winning, commit=False)
            db.commit()
            if outcome == auction_winner.SETTLED:
                paid += 1
                _notify_close(db, auction, piece, winning, _bids_with_status(db, auction.id, BID_LOST))
        except Exception:
            db.rollback()
            logger.exception("Payment retry failed for auction %s", auction_id)
    return paid


def expire_offers(db, now) -> int:
    """Phase 7a: a second-chance offer nobody answered passes to the next bidder."""
    from src.shared.models.auction import AUCTION_SECOND_CHANCE, OFFER_PENDING, AuctionOffer

    due = db.execute(
        select(AuctionOffer.auction_id).join(Auction, Auction.id == AuctionOffer.auction_id).where(
            AuctionOffer.status == OFFER_PENDING, AuctionOffer.expires_at <= now,
            Auction.status == AUCTION_SECOND_CHANCE,
        )
    ).scalars().all()
    moved = 0
    for auction_id in due:
        try:
            auction = db.execute(select(Auction).where(Auction.id == auction_id).with_for_update()).scalar_one()
            offer = db.execute(
                select(AuctionOffer).where(AuctionOffer.auction_id == auction_id,
                                           AuctionOffer.status == OFFER_PENDING).with_for_update()
            ).scalars().first()
            if offer is None or offer.expires_at > now:
                db.rollback()
                continue
            piece = db.get(Piece, auction.piece_id)
            lapsed = offer.bidder_id
            outcome = auction_winner.pass_offer(db, auction, piece, offer, expired=True)
            db.commit()
            moved += 1
            title = piece.title if piece else "a piece"
            _safe_notify(db, user_id=lapsed, type="auction_second_chance_expired",
                         target_id=auction.piece_id, title="Offer expired",
                         body=f'The offer on "{title}" has passed to the next bidder.')
            after_offer(db, auction, piece, outcome)
        except Exception:
            db.rollback()
            logger.exception("Offer expiry failed for auction %s", auction_id)
    return moved


def after_offer(db, auction, piece, outcome) -> None:
    """Tell the next bidder about their offer, or the seller that nobody is left."""
    from src.modules.bids import auction_payments

    title = piece.title if piece else "a piece"
    if outcome == auction_winner.OFFERED:
        offer = auction_payments.pending_offer(db, auction.id)
        if offer is None:
            return
        _offer_notice(db, auction, piece, offer)
        _safe_notify(db, user_id=auction.seller_id, type="auction_second_chance_sent",
                     target_id=auction.piece_id, title="Offered to the next bidder",
                     body=f"The winning bidder couldn't pay for \"{title}\", so we've offered it "
                          f"to the next bidder at ${offer.amount_cents / 100:,.2f}.")
    elif outcome == auction_winner.EXHAUSTED:
        _safe_notify(
            db, user_id=auction.seller_id, type="auction_needs_action",
            target_id=auction.piece_id, title="Your auction needs a decision",
            body=f'No bidder on "{title}" was able to complete payment. Nothing was charged '
                 "to anyone — relist it or set a fixed price.",
        )


def _offer_notice(db, auction, piece, offer) -> None:
    """`auction_second_chance`: push and email (the 9 Nov design, NoteBidSecondChance)."""
    from src.shared.notification.email_service import send_email

    title = piece.title if piece else "a piece"
    amount = f"${offer.amount_cents / 100:,.2f}"
    body = (f'"{title}" is yours for {amount}, your top bid, if you want it. Accept by '
            f'{offer.expires_at.strftime("%-I:%M %p UTC on %b %-d")}.')
    try:
        notifications_dao.create_and_push(
            db, user_id=offer.bidder_id, type="auction_second_chance", target_type="piece",
            target_id=auction.piece_id, title="Second chance", body=body,
            payload={"amountCents": offer.amount_cents, "expiresAt": offer.expires_at.isoformat(),
                     "offerId": str(offer.id)},
        )
    except Exception:
        logger.exception("Second-chance notification failed for %s", offer.bidder_id)
    bidder = db.get(User, offer.bidder_id)
    if bidder is not None and bidder.email:
        send_email(bidder.email, f"Second chance: {title}",
                   f"<p>{body}</p><p>Open Studio 3 to accept and pay with your saved card.</p>")


def expire_winner_windows() -> None:
    """Pass a piece to the next bidder when the winner's window to fix payment has run out.

    Only ``awaiting_payment`` auctions are touched, and that restriction is the safety
    property rather than an optimisation: it is the one state in which the runners-up still
    have money authorised, so it is the one state a cascade can succeed from. A settled sale
    (``awaiting_winner``) has already released them and is never expired — we have taken the
    buyer's money and there is no honest way to un-sell it on a timer.
    """
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        candidates = db.execute(
            select(Auction.id).where(
                Auction.status == AUCTION_AWAITING_PAYMENT,
                Auction.winner_deadline_at.is_not(None),
                Auction.winner_deadline_at <= now,
            )
        ).scalars().all()

        for auction_id in candidates:
            try:
                _expire_one(db, auction_id)
            except Exception:
                db.rollback()
                logger.exception("Failed to expire winner window for auction %s", auction_id)
        # Phase 7a: the automatic retries, and second-chance offers nobody answered.
        retry_failed_payments(db, now)
        expire_offers(db, now)
    finally:
        db.close()


def _expire_one(db, auction_id: uuid.UUID) -> None:
    auction = db.execute(
        select(Auction).where(Auction.id == auction_id).with_for_update()
    ).scalar_one_or_none()
    # Re-checked under the lock. The winner may have fixed their payment in the seconds
    # between the query and here, which moves the auction out of this state entirely.
    if not auction or auction.status != AUCTION_AWAITING_PAYMENT:
        return
    if not auction.winner_deadline_at or auction.winner_deadline_at > datetime.now(timezone.utc):
        return

    piece = db.get(Piece, auction.piece_id)
    forfeited = auction_winner.winning_bid(db, auction)
    outcome = auction_winner.cascade(
        db, auction, piece, reason="winner_payment_window_expired", commit=False
    )
    db.commit()

    title = piece.title if piece else "a piece"
    if forfeited is not None:
        _safe_notify(
            db, user_id=forfeited.bidder_id, type="auction_forfeited",
            target_id=auction.piece_id, title="You've lost this piece",
            body=f'The payment for "{title}" was never completed, so it has gone to another '
                 "bidder. Nothing was charged to you.",
        )

    if outcome == auction_winner.SETTLED:
        new_winner = auction_winner.winning_bid(db, auction)
        if new_winner is not None:
            _notify_close(db, auction, piece, new_winner, _bids_with_status(db, auction.id, BID_LOST))
    elif outcome == auction_winner.PAYMENT_FAILED:
        new_winner = auction_winner.winning_bid(db, auction)
        if new_winner is not None:
            _notify_payment_failed(db, auction, piece, new_winner)
    elif outcome == auction_winner.OFFERED:
        after_offer(db, auction, piece, outcome)
    else:
        _safe_notify(
            db, user_id=auction.seller_id, type="auction_needs_action",
            target_id=auction.piece_id, title="Your auction needs a decision",
            body=f'No bidder on "{title}" was able to complete payment. Nothing was charged '
                 "to anyone — relist it or set a fixed price.",
        )


def _safe_notify(db, *, user_id, type: str, target_id, title: str, body: str) -> None:
    """A notification that must never take the caller down with it."""
    try:
        notifications_dao.create_and_push(
            db, user_id=user_id, type=type, target_type="piece",
            target_id=target_id, title=title, body=body,
        )
    except Exception:
        logger.exception("Notification %s failed for user %s", type, user_id)
