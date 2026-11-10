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
    APPROVAL_APPROVED,
    APPROVAL_DECLINED,
    APPROVAL_WAITING,
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

    # Phase 5 (spec "Artist approval"): the host can tag anyone's work, in any mode, but a
    # piece that isn't theirs waits for its artist — nothing about its listing changes until
    # the artist approves (and may change the price). Pieces the host or the actor owns are
    # approved straight away, as before.
    is_owner = piece.user_id == actor.id or piece.user_id == event.host_id
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
    _require_one_selling_mode(db, event, mode)

    # The destructive half, and only for a piece actually being sold here and approved by
    # its artist. A featured piece is a link on a page; it must not disturb a listing the
    # artist has running elsewhere.
    ended = False
    if mode in (PIECE_SALE, PIECE_BID) and is_owner:
        ended = _end_current_listing(db, piece, actor)

    entry = EventPiece(
        id=uuid.uuid4(),
        event_id=event.id,
        piece_id=piece.id,
        mode=mode,
        price_cents=price_cents if mode != PIECE_FEATURED else None,
        # Pickup is the norm at an event: the work changes hands in the room.
        delivery_mode=(delivery_mode or DELIVERY_PICKUP) if mode != PIECE_FEATURED else None,
        sort_order=sort_order,
        # Spec: "Pieces the host owns are approved automatically"; anyone else's waits for them
        # and is hidden from the event page until approved.
        approval_status=APPROVAL_APPROVED if is_owner else APPROVAL_WAITING,
        requested_by_id=actor.id,
        listing_ended=ended,
    )
    db.add(entry)
    if event.status == "published" and entry.is_selling and entry.is_approved:
        db.flush()
        list_entry(db, event, entry)
    if commit:
        db.commit()
        db.refresh(entry)
    else:
        db.flush()
    return entry


def _require_one_selling_mode(db: Session, event: Event, mode: str) -> None:
    """Spec: "Never both selling and auction in one event"."""
    if mode not in (PIECE_SALE, PIECE_BID):
        return
    other = PIECE_BID if mode == PIECE_SALE else PIECE_SALE
    clash = db.execute(
        select(EventPiece.id).where(EventPiece.event_id == event.id, EventPiece.mode == other,
                                    EventPiece.approval_status != APPROVAL_DECLINED).limit(1)
    ).first()
    if clash:
        raise AppError("An event either sells its pieces or auctions them, not both.", 409)


def event_auction_close(event: Event):
    """When bidding ends: the host's choice, else 30 minutes before the event ends (spec)."""
    from src.modules.bids.auction_dao import EVENT_CLOSE_BUFFER

    return event.auction_ends_at or (event.ends_at - EVENT_CLOSE_BUFFER)


def respond(db: Session, event: Event, entry: EventPiece, artist: User, *, approve: bool,
            price_cents: Optional[int] = None) -> EventPiece:
    """The artist answers a piece request (spec "Artist approval"). Approving a piece for
    sale or auction ends its own listing or auction (bidders are told) and, on a published
    event, lists it for the event at once. Caller commits."""
    piece = db.get(Piece, entry.piece_id)
    if piece is None or piece.user_id != artist.id:
        raise AppError("Piece request not found.", 404)
    if entry.approval_status != APPROVAL_WAITING:
        raise AppError("You've already answered this request.", 409)
    if event.status not in ("draft", "published"):
        raise AppError("This event is no longer being edited.", 409)
    entry.responded_at = datetime.now(timezone.utc)
    if not approve:
        entry.approval_status = APPROVAL_DECLINED
        return entry
    if price_cents is not None and entry.is_selling:
        try:
            price = int(price_cents)
        except (TypeError, ValueError):
            raise AppError("priceCents must be a whole number.", 400) from None
        if price < 100:
            raise AppError("A piece being sold needs a price of at least $1.00.", 400)
        entry.price_cents = price
    if entry.is_selling:
        if piece.status in piece_state.COMMITTED_STATUSES:
            raise AppError("This piece already has a sale in progress and can't be listed again.", 409)
        entry.listing_ended = _end_current_listing(db, piece, artist)
    entry.approval_status = APPROVAL_APPROVED
    if event.status == "published" and entry.is_selling:
        db.flush()
        list_entry(db, event, entry)
    return entry


def _end_current_listing(db: Session, piece: Piece, actor: User) -> bool:
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
        return True

    if piece.is_for_sale and piece.status == piece_state.LIVE:
        piece.is_for_sale = False
        piece_state.transition_piece(
            db, piece, piece_state.DELISTED,
            allowed_from={piece_state.LIVE},
            reason=auction_cancellation.REASON_EVENT_TAGGED,
            commit=False,
        )
        return True
    return False


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


def publish_lineup(
    db: Session, event: Event, commit: bool = True, changes: list | None = None
) -> dict:
    """Create the listings for every approved selling piece on the bill. Called once, at
    publish; a piece approved later is listed when it is approved (`respond`).

    `changes`, when given, collects `(piece, state before, auction or None)` for each listing
    made, so the caller can tell the pieces' savers once it has committed (B12).

    A fixed-price piece goes live at its event price. An auction piece gets an auction whose
    window is the event's: open at the event's start, closed at the host's bidding end (at
    the latest thirty minutes before the event ends), and no soft close — an event auction
    stops dead so the work can be handed over before the room empties.
    """
    listed_fixed = 0
    listed_auction = 0
    if event_auction_close(event) <= event.starts_at and any(
        e.mode == PIECE_BID and e.is_approved for e in list_lineup(db, event.id)
    ):
        raise AppError(
            "This event is too short to run an auction — bidding closes 30 minutes before "
            "the end, which would be before it starts.",
            400,
        )

    for entry in list_lineup(db, event.id):
        if not entry.is_selling or not entry.is_approved:
            continue
        kind = list_entry(db, event, entry, changes=changes)
        if kind == PIECE_SALE:
            listed_fixed += 1
        elif kind == PIECE_BID:
            listed_auction += 1

    if commit:
        db.commit()
    logger.info(
        "Event %s published %d fixed and %d auction listing(s)",
        event.id, listed_fixed, listed_auction,
    )
    return {"fixedListings": listed_fixed, "auctionListings": listed_auction}


def list_entry(db: Session, event: Event, entry: EventPiece, changes: list | None = None):
    """List one approved selling piece for the event. Returns its mode, or None when there
    was nothing to do (already listed, or the piece is gone)."""
    piece = db.get(Piece, entry.piece_id)
    if piece is None:
        return None
    seller = db.get(User, piece.user_id)
    if seller is None:
        return None
    before = None
    if changes is not None:
        from src.modules.notifications import watchers

        before = watchers.listing_state(piece)
    if entry.mode == PIECE_SALE:
        piece.is_for_sale = True
        piece.listing_type = "fixed"
        piece.price_cents = entry.price_cents
        if piece.status in (piece_state.DRAFT, piece_state.DELISTED):
            piece_state.transition_piece(
                db, piece, piece_state.LIVE, reason="event_published", commit=False
            )
        if changes is not None:
            changes.append((piece, before, None))
        return PIECE_SALE

    # PIECE_BID — an auction on the event's clock.
    if auction_dao.get_running_auction(db, piece.id) is not None:
        return None  # already listed by an earlier attempt; listing is idempotent
    closes_at = event_auction_close(event)
    if closes_at <= event.starts_at:
        raise AppError("This event is too short to run an auction.", 400)
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
        # Bidding opens when the doors do, not when the host hits publish — or now, for a
        # piece approved after the event has started.
        opens_at=max(event.starts_at, datetime.now(timezone.utc)),
        closes_at=closes_at,
        commit=False,
    )
    if changes is not None:
        changes.append((piece, before, auction))
    return PIECE_BID


def record_exhibitions(db: Session, event: Event) -> int:
    """B26 / spec "After the event": add Exhibited (event name, venue, month) to every
    approved piece on the bill, once. Caller commits."""
    from src.shared.models.event import PieceExhibition

    added = 0
    for entry in list_lineup(db, event.id):
        if not entry.is_approved:
            continue
        exists = db.execute(
            select(PieceExhibition.id).where(PieceExhibition.piece_id == entry.piece_id,
                                             PieceExhibition.event_id == event.id)
        ).first()
        if exists:
            continue
        db.add(PieceExhibition(id=uuid.uuid4(), piece_id=entry.piece_id, event_id=event.id,
                               title=event.title, venue_name=event.venue_name, city=event.city,
                               starts_at=event.starts_at))
        added += 1
    return added


def exhibitions_for_piece(db: Session, piece_id) -> list[dict]:
    from src.shared.models.event import PieceExhibition

    rows = db.execute(
        select(PieceExhibition).where(PieceExhibition.piece_id == piece_id)
        .order_by(PieceExhibition.starts_at.desc())
    ).scalars()
    return [{
        "eventId": str(r.event_id) if r.event_id else None,
        "title": r.title,
        "venueName": r.venue_name,
        "city": r.city,
        "date": r.starts_at.isoformat(),
        "month": r.starts_at.strftime("%B %Y"),
    } for r in rows]


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
