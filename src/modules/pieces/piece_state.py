"""The single writer of `piece.status`.

Mirrors orders_dao.transition_order: one explicit map of legal moves, one choke point, so a
status can never be set to an arbitrary string and no caller can invent a transition the
rest of the system does not expect. Before this existed, pieces_controller.patch assigned
`piece.status = body["status"]` with no whitelist at all, which meant a piece owner could
hand themselves `auction_won` — the exact state auction_checkout trusts.

Kept in its own module rather than pieces_dao because orders_dao, webhook_handler,
auction_closer and user_dao all need it, and pieces_dao imports none of them. A choke point
with no inbound dependencies cannot create an import cycle.
"""
from typing import Optional

from sqlalchemy.orm import Session

from src.shared.models.piece import Piece
from src.shared.utils.app_error import AppError

DRAFT = "draft"
LIVE = "live"
RESERVED = "reserved"
SOLD = "sold"
AUCTION_WON = "auction_won"
DELISTED = "delisted"
DELETED = "deleted"

# `deleted` is written by pieces_dao.delete_piece but was missing from the model's
# documented enum; it belongs here so the set is actually complete.
PIECE_STATUSES = (DRAFT, LIVE, RESERVED, SOLD, AUCTION_WON, DELISTED, DELETED)

# Statuses in which a piece is publicly visible — feeds, profile grids, search. Exported so
# feeds_controller reads it from here instead of keeping a second list that can drift.
PUBLIC_STATUSES = (LIVE, RESERVED, SOLD, AUCTION_WON)

VALID_TRANSITIONS: dict[str, set[str]] = {
    DRAFT: {LIVE, DELETED},
    LIVE: {RESERVED, AUCTION_WON, DELISTED, DRAFT, DELETED},
    # A reserved piece goes to sold on payment, or back to live if the order falls through.
    RESERVED: {SOLD, LIVE, AUCTION_WON, DELISTED},
    # sold -> live is the refund path: release_pieces puts the work back on the market.
    SOLD: {LIVE, AUCTION_WON, DELISTED},
    AUCTION_WON: {RESERVED, DELISTED},
    DELISTED: {LIVE, DRAFT, DELETED},
    DELETED: set(),
}

# Statuses that mean money or a winner is already attached, so the listing terms are frozen.
COMMITTED_STATUSES = (RESERVED, SOLD, AUCTION_WON)


def apply_auction_clock(piece: Piece) -> None:
    """Retained as a no-op hook.

    The auction clock used to live on the piece, which meant a piece that had been through an
    auction kept its old end time forever and a relisted one expired the moment it went live.
    Timing now belongs to the Auction row, which is created fresh for each run — so there is
    nothing on the piece left to keep in step. Kept as a named seam because transition_piece
    calls it on every entry into `live`, and a future listing type may want it again.
    """
    return None


def transition_piece(
    db: Session,
    piece: Piece,
    new_status: str,
    *,
    allowed_from: Optional[set] = None,
    reason: Optional[str] = None,
    commit: bool = True,
) -> Piece:
    """Move a piece to `new_status`, enforcing VALID_TRANSITIONS.

    `allowed_from` narrows the legal sources further for one caller — the auction closer
    only ever acts on a live piece, for instance, and should fail rather than quietly
    re-close something already sold.

    Pass commit=False when the change has to land atomically with other writes (booking a
    ledger transaction, creating an order); the caller commits once.
    """
    current = piece.status
    if current == new_status:
        # Idempotent: a duplicate webhook or a re-run scheduler tick is a no-op, not an error.
        return piece

    if new_status not in PIECE_STATUSES:
        raise AppError(f"Unknown piece status '{new_status}'.", 400)
    if allowed_from is not None and current not in allowed_from:
        raise AppError(f"Cannot move this piece from '{current}' to '{new_status}'.", 409)
    if new_status not in VALID_TRANSITIONS.get(current, set()):
        raise AppError(f"Cannot move this piece from '{current}' to '{new_status}'.", 409)

    # Withdrawing an auction that people are actively bidding on is not a status change —
    # it is a cancellation, which has to release holds and tell the bidders. Routing it
    # through here instead would strand them silently, which is what user_dao.delist_user_pieces
    # used to do. auction_cancellation.cancel_auction voids the bids first, so by the time it
    # calls this the count is zero and the guard passes.
    if current == LIVE and new_status in (DELISTED, DRAFT) and piece.listing_type == "auction":
        from src.modules.bids import bid_dao

        if bid_dao.count_active_bids_for_piece(db, piece.id) > 0:
            raise AppError(
                "This auction has active bids. Cancel the auction instead of delisting it.",
                409,
            )

    piece.status = new_status
    apply_auction_clock(piece)

    if reason:
        # Not persisted — piece history is out of scope here — but it makes the log line
        # explain *why* a piece moved, which is most of the value during an incident.
        from src.shared.utils.logger import get_logger

        get_logger(__name__).info(
            "Piece %s: %s -> %s (%s)", piece.id, current, new_status, reason
        )

    if commit:
        db.commit()
        db.refresh(piece)
    return piece


def release_target_status(db: Session, piece: Piece) -> str:
    """Where a piece returns to when its order is cancelled or refunded.

    Fixed-price work goes back on sale. An auction must not: `live` would reopen bidding on
    an auction whose close time has already passed, so the sweep would immediately re-close
    it. It returns to the winner instead, or is delisted if no winning bid survives.
    """
    if piece.listing_type != "auction":
        return LIVE
    from src.modules.bids import auction_dao, bid_dao

    auction = auction_dao.get_running_auction(db, piece.id)
    if auction is None:
        return DELISTED
    return AUCTION_WON if bid_dao.get_highest_bid(db, auction.id) else DELISTED
