"""Keep a piece's Auction in step with its listing after an edit (BACKEND_CHANGES B16).

`PATCH /api/pieces/<id>` used to validate auction terms and then never touch the Auction row,
which only create, relist and event publish wrote. So a piece listed at auction later had no
Auction and every bid failed; a draft auction taken live stayed in draft; a new starting bid,
reserve or length never reached the Auction; and an auction switched to fixed price kept
running until the closer delisted the now fixed-price piece.

`sync` runs inside the PATCH, after the piece's own fields and status are set and before the
commit, so the piece and its auction change together or not at all:

* Listed at auction with no Auction yet: one is created, in draft.
* Terms edited while it has no bids: the starting bid, reserve and length move to the Auction.
* The piece is live: a draft Auction opens (the clock starts now, as at create).
* The piece leaves live (draft or delisted): the run is cancelled and a fresh draft with the
  same terms waits, so going live again starts a new clock rather than reopening an old one.
* No longer an auction (fixed price, or not for sale): the running Auction is cancelled.

Bids freeze all of this: `listing_rules.assert_terms_editable` and `transition_piece` already
refuse term changes and delisting once there are bids, and this module checks again rather than
trusting it. An event's auction belongs to the event, so its terms are refused here and its
clock is left alone.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.modules.bids import auction_dao, bid_dao
from src.modules.pieces import listing_rules, piece_state
from src.shared.models.auction import (
    AUCTION_CANCELLED,
    AUCTION_CLOSING,
    AUCTION_DRAFT,
    AUCTION_LIVE,
    Auction,
)
from src.shared.models.piece import Piece
from src.shared.models.user import User
from src.shared.utils.app_error import AppError

# `auctions.cancelled_reason` values for runs ended by an edit rather than by the seller's
# Cancel auction.
REASON_LISTING_CHANGED = "listing_changed"
REASON_UNLISTED = "unlisted"


def current_reserve(db: Session, piece: Optional[Piece]) -> Optional[int]:
    """The reserve on the piece's running auction, so a patch that omits reserveCents keeps it
    (and is checked against a new starting bid) instead of reading None."""
    if piece is None:
        return None
    auction = auction_dao.get_running_auction(db, piece.id)
    return auction.reserve_cents if auction else None


def assert_not_event_auction(db: Session, piece: Piece) -> None:
    """An event's auction takes its terms and window from the event."""
    auction = auction_dao.get_running_auction(db, piece.id)
    if auction is not None and auction.event_id is not None:
        raise AppError(
            "This piece is auctioned at an event, so change its terms from the event.", 409
        )


def _has_bids(db: Session, auction: Auction) -> bool:
    return bid_dao.count_active_bids(db, auction.id) > 0


def _end(db: Session, auction: Auction, reason: str) -> None:
    """Cancel a run nobody has bid on. Refuses one with bids: those go through Cancel auction,
    which voids the bids, releases the holds and tells the bidders."""
    if _has_bids(db, auction):
        raise AppError(
            "This auction already has bids, so its terms can't be changed. "
            "Cancel the auction instead.",
            409,
        )
    auction.status = AUCTION_CANCELLED
    auction.cancelled_reason = reason[:64]
    auction.closed_at = datetime.now(timezone.utc)
    db.flush()


def _update_terms(db: Session, auction: Auction, listing: dict) -> None:
    starting = listing["price_cents"]
    reserve = listing["reserve_cents"]
    days = listing["auction_duration_days"]
    if (auction.starting_bid_cents, auction.reserve_cents, auction.duration_days) == (
        starting, reserve, days,
    ):
        return
    if auction.status == AUCTION_CLOSING or _has_bids(db, auction):
        raise AppError(
            "This auction already has bids, so its terms can't be changed. "
            "Cancel the auction instead.",
            409,
        )
    if auction.status == AUCTION_LIVE and days != auction.duration_days:
        if auction.extended_once:
            raise AppError("This auction has been extended, so its length can't be changed.", 409)
        closes_at = auction.opens_at + timedelta(days=days)
        if closes_at <= datetime.now(timezone.utc):
            raise AppError(
                f"This auction has already run longer than {days} days. Choose a longer one.",
                400,
            )
        auction.closes_at = closes_at
    auction.starting_bid_cents = starting
    auction.reserve_cents = reserve
    auction.duration_days = days


def _draft(db: Session, piece: Piece, seller: User, *, starting, days, reserve) -> Auction:
    return auction_dao.create_auction(
        db,
        piece,
        seller,
        starting_bid_cents=starting,
        duration_days=days,
        reserve_cents=reserve,
        commit=False,
    )


def _last_terms(db: Session, piece: Piece) -> Optional[dict]:
    """The terms of the piece's most recent standalone run (ended, or cancelled by an edit), for
    a piece set live again without new terms."""
    last = db.execute(
        select(Auction)
        .where(Auction.piece_id == piece.id, Auction.event_id.is_(None))
        .order_by(Auction.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last is None or last.duration_days is None:
        return None
    return dict(starting=last.starting_bid_cents, days=last.duration_days, reserve=last.reserve_cents)


def sync(db: Session, piece: Piece, seller: User, listing: Optional[dict]) -> Optional[Auction]:
    """Bring the piece's Auction in line with the piece. `listing` is the validated, merged
    listing when the patch changed terms, else None. Returns the Auction if this call opened
    it, so the caller can tell the piece's savers once it has committed (B12)."""
    running = auction_dao.get_running_auction(db, piece.id, lock=True)
    if running is not None and running.event_id is not None:
        return None

    if not (piece.is_for_sale and piece.listing_type == listing_rules.LISTING_AUCTION):
        if running is not None:
            _end(db, running, REASON_LISTING_CHANGED)
        return None

    if running is None:
        if listing is None and piece.status != piece_state.LIVE:
            # A status change off the profile with no auction to end: nothing to keep in step.
            return None
        if listing is not None:
            terms = dict(
                starting=listing["price_cents"],
                days=listing["auction_duration_days"],
                reserve=listing["reserve_cents"],
            )
        else:
            terms = _last_terms(db, piece)
            if terms is None:
                # Listed at auction before B16, so no Auction was ever made and its length was
                # only on that request: ask for it.
                raise AppError(
                    "Auction duration must be between "
                    f"{auction_dao.MIN_DURATION_DAYS} and {auction_dao.MAX_DURATION_DAYS} days.",
                    400,
                )
        running = _draft(db, piece, seller, **terms)
    elif listing is not None:
        _update_terms(db, running, listing)

    if piece.status == piece_state.LIVE:
        if running.status == AUCTION_DRAFT:
            auction_dao.open_auction(db, running, commit=False)
            return running
        return None

    if running.status in (AUCTION_LIVE, AUCTION_CLOSING):
        # Off the profile (draft or delisted): this run ends; the next one starts when the piece
        # is live again.
        terms = dict(
            starting=running.starting_bid_cents,
            days=running.duration_days,
            reserve=running.reserve_cents,
        )
        _end(db, running, REASON_UNLISTED)
        _draft(db, piece, seller, **terms)
    return None
