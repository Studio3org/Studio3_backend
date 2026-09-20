"""Putting work on an event's bill, and what that does to the work's existing listing.

This file exists for one rule the client was explicit about: **tagging a piece to an event
ends whatever listing it already had.** A work cannot be for sale in two places at once, so
moving it onto an event closes its current fixed-price listing or cancels its running
auction — which releases every bidder's hold and tells them the seller withdrew it.

That is destructive and irreversible, which shapes everything here:

* **It happens when the lineup entry is added, not at publish.** The host is told what will
  happen while they are still in the create flow and can change their mind, rather than
  discovering at publish that they have refunded four bidders.
* **Only the piece's owner can do it.** The featured-pieces picker offers work by every
  artist on the bill, but ending another artist's auction and refunding their bidders is not
  a host's decision to make. A host may add anyone's work as `featured`, which changes
  nothing about its listing; `sale` and `bid` are the owner's alone.
* **Publishing creates the new listings.** A draft event lists nothing, so a host can build
  and rearrange a bill without anything going on sale. An event auction takes its window
  from the event: it opens when the event opens and closes thirty minutes before the event
  ends, with no soft close, because the room empties and the piece has to change hands that
  night.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.shared.models.event import (
    DELIVERY_PICKUP,
    EVENT_PIECE_MODES,
    PIECE_BID,
    PIECE_FEATURED,
    PIECE_SALE,
    Event,
    EventPiece,
)
from src.shared.models.piece import Piece
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger
from src.modules.bids import auction_cancellation, auction_dao
from src.modules.pieces import piece_state

logger = get_logger(__name__)


def list_lineup(db: Session, event_id: uuid.UUID) -> list[EventPiece]:
    return list(
        db.execute(
            select(EventPiece)
            .where(EventPiece.event_id == event_id)
            .order_by(EventPiece.sort_order.asc(), EventPiece.created_at.asc())
        ).scalars()
    )


def preview_tagging(db: Session, piece: Piece) -> dict:
    """What adding this piece to a bill would cost, without doing it.

    The create flow asks before it acts: "this ends the auction you have running, and the
    four people bidding on it get their money back". That sentence needs real numbers, and
    they have to come from the same place the action does or the warning drifts from the
    consequence.
    """
    from src.modules.bids import bid_dao

    auction = auction_dao.get_running_auction(db, piece.id)
    if auction is not None:
        return {
            "endsAuction": True,
            "endsFixedListing": False,
            "activeBidCount": bid_dao.count_active_bids(db, auction.id),
        }
    ends_fixed = bool(piece.is_for_sale and piece.status == piece_state.LIVE)
    return {"endsAuction": False, "endsFixedListing": ends_fixed, "activeBidCount": 0}


def add_piece(
    db: Session,
    event: Event,
    piece: Piece,
    actor: User,
    *,
    mode: str = PIECE_FEATURED,
    price_cents: Optional[int] = None,
    delivery_mode: Optional[str] = None,
    sort_order: int = 0,
    commit: bool = True,
) -> EventPiece:
    """Add a piece to an event's bill, ending its current listing if it is being sold there.

    Raises 403 when someone other than the owner tries to sell another artist's work, and
    409 when the piece has a sale already in flight — a reserved or sold piece is not the
    artist's to move any more.
    """
    if mode not in EVENT_PIECE_MODES:
        raise AppError(f"Unknown lineup mode '{mode}'.", 400)
    if event.status not in ("draft", "published"):
        raise AppError("This event is no longer being edited.", 409)

    is_owner = piece.user_id == actor.id
    if mode != PIECE_FEATURED and not is_owner:
        # The line drawn in this module's docstring. A host may credit and display anyone's
        # work; selling it — and cancelling whatever auction it is in, refunding those
        # bidders — belongs to the artist.
        raise AppError(
            "Only the artist can put their own work up for sale at an event. "
            "Add it as a featured piece, or ask them to list it.",
            403,
        )
    if mode != PIECE_FEATURED:
        if price_cents is None or price_cents < 100:
            raise AppError("A piece being sold needs a price of at least $1.00.", 400)
        if piece.status in piece_state.COMMITTED_STATUSES:
            raise AppError(
                "This piece already has a sale in progress and can't be listed again.", 409
            )

    existing = db.execute(
        select(EventPiece).where(
            EventPiece.event_id == event.id, EventPiece.piece_id == piece.id
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise AppError("This piece is already on the bill.", 409)

    # The destructive half, and only for a piece actually being sold here. A featured piece
    # is a link on a page; it must not disturb a listing the artist has running elsewhere.
    if mode in (PIECE_SALE, PIECE_BID):
        _end_current_listing(db, piece, actor)

    entry = EventPiece(
        id=uuid.uuid4(),
        event_id=event.id,
        piece_id=piece.id,
        mode=mode,
        price_cents=price_cents if mode != PIECE_FEATURED else None,
        # Pickup is the norm at an event: the work changes hands in the room.
        delivery_mode=(delivery_mode or DELIVERY_PICKUP) if mode != PIECE_FEATURED else None,
        sort_order=sort_order,
    )
    db.add(entry)
    if commit:
        db.commit()
        db.refresh(entry)
    else:
        db.flush()
    return entry


def _end_current_listing(db: Session, piece: Piece, actor: User) -> None:
    """Close whatever this piece is currently listed as.

    Auctions go through cancel_auction rather than a status write, so every hold is released
    and every bidder is told. Doing it by hand here is exactly the bug that stranded bidders
    when seller deactivation delisted live auctions directly.
    """
    auction = auction_dao.get_running_auction(db, piece.id)
    if auction is not None:
        auction_cancellation.cancel_auction(
            db,
            piece,
            auction_cancellation.REASON_EVENT_TAGGED,
            actor_id=actor.id,
            target_status=piece_state.DELISTED,
            # The caller commits — adding to a bill and ending the old listing are one
            # change, and a crash between them would leave a cancelled auction with nothing
            # to show for it.
            commit=False,
        )
        return

    if piece.is_for_sale and piece.status == piece_state.LIVE:
        piece.is_for_sale = False
        piece_state.transition_piece(
            db, piece, piece_state.DELISTED,
            allowed_from={piece_state.LIVE},
            reason=auction_cancellation.REASON_EVENT_TAGGED,
            commit=False,
        )


def remove_piece(db: Session, event: Event, entry: EventPiece, commit: bool = True) -> None:
    """Take a piece off the bill.

    Deliberately does **not** restore the listing it replaced. That listing was cancelled,
    its bids voided and its bidders refunded and notified; silently resurrecting it would
    reopen an auction those people have already been told is over. The artist relists.
    """
    if event.status == "published" and entry.is_selling:
        raise AppError(
            "This piece is already listed for the event. Cancel the event listing instead.",
            409,
        )
    db.delete(entry)
    if commit:
        db.commit()


def publish_lineup(db: Session, event: Event, commit: bool = True) -> dict:
    """Create the listings for every selling piece on the bill. Called once, at publish.

    A fixed-price piece goes live at its event price. An auction piece gets an auction whose
    window is the event's: open at the event's start, closed thirty minutes before it ends,
    and no soft close — an event auction stops dead so the work can be handed over before
    the room empties.
    """
    from src.modules.bids.auction_dao import EVENT_CLOSE_BUFFER

    listed_fixed = 0
    listed_auction = 0
    closes_at = event.ends_at - EVENT_CLOSE_BUFFER
    if closes_at <= event.starts_at:
        raise AppError(
            "This event is too short to run an auction — bidding closes 30 minutes before "
            "the end, which would be before it starts.",
            400,
        )

    for entry in list_lineup(db, event.id):
        if not entry.is_selling:
            continue
        piece = db.get(Piece, entry.piece_id)
        if piece is None:
            continue
        seller = db.get(User, piece.user_id)
        if seller is None:
            continue

        if entry.mode == PIECE_SALE:
            piece.is_for_sale = True
            piece.listing_type = "fixed"
            piece.price_cents = entry.price_cents
            if piece.status in (piece_state.DRAFT, piece_state.DELISTED):
                piece_state.transition_piece(
                    db, piece, piece_state.LIVE, reason="event_published", commit=False
                )
            listed_fixed += 1
            continue

        # PIECE_BID — an auction on the event's clock.
        if auction_dao.get_running_auction(db, piece.id) is not None:
            # Already listed by an earlier publish attempt. Publishing is idempotent.
            continue
        piece.is_for_sale = True
        piece.listing_type = "auction"
        piece.price_cents = entry.price_cents
        auction = auction_dao.create_auction(
            db, piece, seller,
            starting_bid_cents=entry.price_cents,
            event_id=event.id,
            delivery_mode=entry.delivery_mode or DELIVERY_PICKUP,
            commit=False,
        )
        if piece.status in (piece_state.DRAFT, piece_state.DELISTED):
            piece_state.transition_piece(
                db, piece, piece_state.LIVE, reason="event_published", commit=False
            )
        auction_dao.open_auction(
            db, auction,
            # Bidding opens when the doors do, not when the host hits publish.
            opens_at=event.starts_at,
            closes_at=closes_at,
            commit=False,
        )
        listed_auction += 1

    if commit:
        db.commit()
    logger.info(
        "Event %s published %d fixed and %d auction listing(s)",
        event.id, listed_fixed, listed_auction,
    )
    return {"fixedListings": listed_fixed, "auctionListings": listed_auction}


def cancel_lineup(db: Session, event: Event, reason: str, commit: bool = True) -> dict:
    """Undo an event's listings when the event itself is cancelled.

    Auctions are cancelled properly — holds released, bidders told — rather than left
    running against an event that is not happening. A fixed-price listing simply comes down.
    """
    cancelled_auctions = 0
    delisted = 0
    now = datetime.now(timezone.utc)

    for entry in list_lineup(db, event.id):
        if not entry.is_selling:
            continue
        piece = db.get(Piece, entry.piece_id)
        if piece is None:
            continue
        # A piece with a sale in flight is not the event's to withdraw any more — somebody
        # has bought it and there is an order pointing at it.
        if piece.status in piece_state.COMMITTED_STATUSES:
            continue

        auction = auction_dao.get_running_auction(db, piece.id)
        if auction is not None:
            auction_cancellation.cancel_auction(
                db, piece, reason, actor_id=event.host_id,
                target_status=piece_state.DELISTED, commit=False,
            )
            cancelled_auctions += 1
        elif piece.status == piece_state.LIVE:
            piece.is_for_sale = False
            piece_state.transition_piece(
                db, piece, piece_state.DELISTED, allowed_from={piece_state.LIVE},
                reason=reason, commit=False,
            )
            delisted += 1

    event.cancelled_at = now
    if commit:
        db.commit()
    return {"cancelledAuctions": cancelled_auctions, "delisted": delisted}
