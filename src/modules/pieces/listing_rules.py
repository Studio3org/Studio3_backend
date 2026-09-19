"""Fixed-vs-auction invariants, in one place.

A piece sells one way or the other, never both. That rule was previously enforced
ad hoc — bidding checked it, checkout did not — which is how `POST /pieces/<id>/collect`
came to buy a live auction outright at its starting bid, bypassing every bid placed.

`price_cents` carries the fixed price on one and the starting bid on the other, which is
exactly why the guard matters: on an auction that number is the floor for bidding, not a
price anyone may pay.
"""
from src.shared.models.piece import Piece
from src.shared.utils.app_error import AppError
from src.modules.pieces import piece_state

LISTING_FIXED = "fixed"
LISTING_AUCTION = "auction"
LISTING_TYPES = (LISTING_FIXED, LISTING_AUCTION)

MIN_AUCTION_DAYS = 3
MAX_AUCTION_DAYS = 14
MIN_PRICE_CENTS = 100

# Listing terms — frozen once a sale is in flight, or once an auction has real bids against
# them. Descriptive fields (title, caption, images) stay editable throughout.
COMMERCIAL_FIELDS = (
    "isForSale",
    "listingType",
    "priceCents",
    "auctionDurationDays",
)

# Statuses a piece owner may set directly. Everything else — reserved, sold, auction_won,
# deleted — is driven by money or by the auction closer and is only ever written through
# piece_state.transition_piece by those flows.
OWNER_DRIVEN_STATUSES = (piece_state.DRAFT, piece_state.LIVE, piece_state.DELISTED)


def assert_purchasable_fixed(piece: Piece) -> None:
    """Preconditions for an outright purchase. Raises rather than returning a bool so no
    caller can forget to check the result."""
    if not piece.is_for_sale or piece.status != piece_state.LIVE:
        raise AppError("This piece is no longer available.", 409)
    if piece.listing_type != LISTING_FIXED:
        raise AppError("This piece is sold by auction — place a bid instead.", 409)


def assert_biddable(piece: Piece) -> None:
    """Preconditions for placing a bid. The mirror image of assert_purchasable_fixed."""
    if not piece.is_for_sale or piece.listing_type != LISTING_AUCTION:
        raise AppError("This piece is not up for auction.", 400)
    if piece.status != piece_state.LIVE:
        raise AppError("This piece is no longer available.", 409)


def validate_listing(
    *,
    is_for_sale: bool,
    listing_type,
    price_cents,
    auction_duration_days,
    seller_enabled: bool,
    seller=None,
    reserve_cents=None,
) -> dict:
    """Validate a complete listing shape and return it normalized.

    Takes the *merged* result of a patch rather than the raw body, so a lone
    `{"priceCents": 5000}` on an auction is still checked against the auction rules instead
    of slipping through because `isForSale` happened not to be in the request.
    """
    if not is_for_sale:
        # Turning sale off clears every sale-only field, including the auction clock —
        # leaving a stale auction_ends_at behind made a relisted piece expire instantly.
        return {
            "is_for_sale": False,
            "listing_type": None,
            "price_cents": price_cents,
            "auction_duration_days": None,
            "reserve_cents": None,
        }

    if not seller_enabled:
        raise AppError("Enable seller mode before listing for sale.", 403)
    # FR-1.3: a piece can't go on sale until the artist can actually be paid.
    if seller is not None and not seller.stripe_payouts_enabled:
        raise AppError(
            "Finish payout setup before listing work for sale, so we can pay you when it sells.",
            403,
        )

    normalized_type = (listing_type or LISTING_FIXED).strip().lower()
    if normalized_type not in LISTING_TYPES:
        raise AppError(f"listingType must be one of: {', '.join(LISTING_TYPES)}.", 400)

    days = None
    if normalized_type == LISTING_AUCTION:
        try:
            days = int(auction_duration_days)
        except (TypeError, ValueError):
            raise AppError(
                f"Auction duration must be between {MIN_AUCTION_DAYS} and "
                f"{MAX_AUCTION_DAYS} days.",
                400,
            ) from None
        if days < MIN_AUCTION_DAYS or days > MAX_AUCTION_DAYS:
            raise AppError(
                f"Auction duration must be between {MIN_AUCTION_DAYS} and "
                f"{MAX_AUCTION_DAYS} days.",
                400,
            )

    try:
        price = int(price_cents)
    except (TypeError, ValueError):
        price = 0
    if price < MIN_PRICE_CENTS:
        raise AppError("Price must be at least $1.00 (100 cents).", 400)

    reserve = None
    if normalized_type == LISTING_AUCTION and reserve_cents not in (None, ""):
        try:
            reserve = int(reserve_cents)
        except (TypeError, ValueError):
            raise AppError("Reserve must be a whole number of cents.", 400) from None
        # Never surfaced to bidders — they see met / not met — but it has to be reachable,
        # or the auction can never complete.
        if reserve < price:
            raise AppError("A reserve cannot be below the starting bid.", 400)

    return {
        "is_for_sale": True,
        "listing_type": normalized_type,
        "price_cents": price,
        "auction_duration_days": days,
        "reserve_cents": reserve,
    }


def assert_terms_editable(db, piece: Piece) -> None:
    """Refuse changes to listing terms once they are committed to.

    Two cases, both of which used to be accepted silently: a piece with a sale in flight,
    and a live auction people have already bid against. Changing the type or price of an
    auction with bids orphans those bids — they stay attached to a piece that is no longer
    being auctioned.
    """
    if piece.status in piece_state.COMMITTED_STATUSES:
        raise AppError(
            "This piece has a sale in progress, so its listing details can't be changed.",
            409,
        )
    if piece.listing_type == LISTING_AUCTION and piece.status == piece_state.LIVE:
        from src.modules.bids import bid_dao

        if bid_dao.count_active_bids_for_piece(db, piece.id) > 0:
            raise AppError(
                "This auction already has bids, so its terms can't be changed. "
                "Cancel the auction instead.",
                409,
            )
