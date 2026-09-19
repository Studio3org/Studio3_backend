"""Keeping a long auction's money authorised.

A card authorisation does not last forever. Stripe's window is around seven days and varies
by network, while a standalone auction can run fourteen — so a hold placed on day one is
dead long before the auction closes, and the close would capture nothing.

This sweep re-authorises holds before they lapse. It runs nightly, which is ample against a
window measured in days, and deliberately not more often: every re-authorisation is another
chance for an issuer to decline, and re-authorising a card more than it needs is how a
bidder gets dropped from an auction they were winning.

Event auctions never reach this. Their window is hours.

**What happens when a refresh fails** is the part worth being careful about. The bidder is
not dropped: the bid stands, the hold is marked so the close knows not to rely on it, and
the bidder is told to add another card. Voiding the bid here would silently remove someone
from a live auction on the strength of one issuer decline, which is both wrong and
unrecoverable — their place in the ordering cannot be given back.
"""
from datetime import datetime, timedelta, timezone

from src.shared.config.database import SessionLocal
from src.shared.models.auction import HOLD_EXPIRED, Hold
from src.shared.models.bid import Bid
from src.shared.models.user import User
from src.shared.utils.logger import get_logger
from src.modules.bids import holds_service
from src.modules.notifications import notifications_dao

logger = get_logger(__name__)

# How far ahead of the deadline to renew. Two days against a roughly seven-day window means
# a failure still leaves two more nightly passes to recover before the money actually lapses.
REFRESH_LEAD_TIME = timedelta(days=2)


def refresh_expiring_holds() -> None:
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) + REFRESH_LEAD_TIME
        due = holds_service.holds_needing_refresh(db, cutoff)
        if not due:
            logger.debug("No holds need re-authorising.")
            return

        logger.info("Re-authorising %d expiring hold(s).", len(due))
        for hold in due:
            try:
                _refresh_one(db, hold.id)
            except Exception:
                db.rollback()
                logger.exception("Hold refresh failed for %s", hold.id)
    finally:
        db.close()


def _refresh_one(db, hold_id) -> None:
    from sqlalchemy import select

    hold = db.execute(
        select(Hold).where(Hold.id == hold_id).with_for_update()
    ).scalar_one_or_none()
    # Re-read under the lock: between the query and here the auction may have closed, which
    # captures or releases this hold. Re-authorising either would put money back on a card
    # for a sale that is already over.
    if not hold or hold.status not in ("pending", "held"):
        return

    bidder = db.get(User, hold.bidder_id)
    if bidder is None:
        logger.error("Hold %s has no bidder", hold.id)
        return

    try:
        holds_service.reauthorize(db, hold, bidder, commit=False)
        db.commit()
        logger.info("Re-authorised hold %s for bidder %s", hold.id, bidder.id)
        return
    except Exception as exc:
        logger.warning("Could not re-authorise hold %s: %s", hold.id, exc)
        failure = str(exc)[:1000]

    # The bid survives; only its funding is in question. Marking the hold rather than
    # cancelling the bid keeps the bidder in the auction and gives them a way back in.
    #
    # The refresh_count that reauthorize bumped before failing is kept deliberately: it is
    # what makes the next attempt's idempotency key distinct from the one that just failed.
    hold.status = HOLD_EXPIRED
    hold.last_error = failure
    db.commit()
    _notify_expired(db, hold, bidder)


def _notify_expired(db, hold: Hold, bidder: User) -> None:
    bid = db.get(Bid, hold.bid_id)
    try:
        notifications_dao.create_and_push(
            db,
            user_id=bidder.id,
            type="bid_hold_expired",
            target_type="piece",
            target_id=None,
            payload={"amountCents": hold.amount_cents, "bidId": str(bid.id) if bid else None},
            title="Update your card to stay in this auction",
            body="We couldn't renew the hold on your card. Your bid still stands, but you'll "
                 "need to add another card before the auction closes.",
        )
    except Exception:
        logger.exception("Hold-expiry notification failed for bidder %s", bidder.id)
