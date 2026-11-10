"""Shipping quotes and estimates (Phase 8; Convelio plan D-2, D-3, B19).

* **Quote (checkout).** The buyer picks an address, speed, delivery type and insurance; we
  ask Convelio for a quote and store it. An instant price (`created`) can be bought straight
  away. A custom quote (`processing`) is priced by Convelio within 24 hours: nothing is
  reserved (D-2), and the buyer is told when it's `ready`. A quote can be bought on for
  SHIPPING_QUOTE_VALID_HOURS.
* **Estimate (piece page).** Convelio's estimate when it gives one; otherwise a range from a
  size-and-weight table (decision S-1), until Convelio enables US estimates.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.modules.shipping import convelio_client, convelio_mapper, shipping_config
from src.shared.models.address import Address
from src.shared.models.piece import Piece
from src.shared.models.shipping import (
    BUYABLE_QUOTE_STATUSES,
    DELIVERY_TYPES,
    QUOTE_CREATED,
    QUOTE_EXPIRED,
    QUOTE_PROCESSING,
    QUOTE_READY,
    SHIPPING_SPEEDS,
    ShippingQuote,
)
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def pickup_address(db: Session, artist: User) -> Optional[Address]:
    if artist is None or not getattr(artist, "pickup_address_id", None):
        return None
    return db.get(Address, artist.pickup_address_id)


def request_quote(db: Session, *, piece: Piece, buyer: User, address: Address, speed: str,
                  delivery_type: str, insurance: Optional[bool]) -> ShippingQuote:
    if not shipping_config.enabled():
        raise AppError("Live shipping quotes aren't switched on yet.", 409)
    if speed not in SHIPPING_SPEEDS:
        raise AppError(f"speed must be one of: {', '.join(SHIPPING_SPEEDS)}.", 400)
    if delivery_type not in DELIVERY_TYPES:
        raise AppError(f"deliveryType must be one of: {', '.join(DELIVERY_TYPES)}.", 400)
    if address.user_id != buyer.id:
        raise AppError("Address not found.", 404)
    insurance = shipping_config.insurance_default() if insurance is None else bool(insurance)
    artist = db.get(User, piece.user_id)
    quote = ShippingQuote(id=uuid.uuid4(), piece_id=piece.id, buyer_id=buyer.id, address_id=address.id,
                          shipping_speed=speed, delivery_type=delivery_type, insurance=insurance)
    body = convelio_mapper.quote_body(
        piece=piece, artist=artist, pickup_address=pickup_address(db, artist), buyer=buyer,
        delivery_address=address, speed=speed, delivery_type=delivery_type, insurance=insurance,
        reference=f"studio3-quote-{quote.id}",
    )
    result = convelio_client.create_quote(body)
    _apply(quote, result)
    quote.request_snapshot = body
    db.add(quote)
    db.commit()
    return quote


def _apply(quote: ShippingQuote, result: dict) -> None:
    quote.provider_quote_id = result.get("id") or quote.provider_quote_id
    quote.provider_ref = result.get("quote_reference_number") or quote.provider_ref
    quote.currency = ((result.get("price") or {}).get("currency_code") or "USD").upper()
    for key, value in convelio_mapper.price_fields(result.get("price")).items():
        setattr(quote, key, value)
    priced = result.get("status") == QUOTE_CREATED and quote.price_incl_cents is not None
    if not priced:
        quote.status = QUOTE_PROCESSING
        return
    # A quote we already had as pending is a custom quote Convelio has now priced.
    quote.status = QUOTE_READY if quote.status == QUOTE_PROCESSING else QUOTE_CREATED
    quote.ready_at = quote.ready_at or _now()
    quote.expires_at = _now() + timedelta(hours=shipping_config.quote_valid_hours())
    if quote.currency != "USD":
        raise AppError("Shipping came back in the wrong currency.", 502)


def refresh(db: Session, quote: ShippingQuote) -> ShippingQuote:
    """Read a custom quote again once Convelio says it's ready."""
    result = convelio_client.get_quote(quote.provider_quote_id)
    _apply(quote, result)
    db.commit()
    return quote


def buyable(db: Session, quote_id, buyer: User, piece: Piece) -> ShippingQuote:
    """The quote this checkout pays, re-read on the server: the app never sends a price."""
    try:
        quote = db.get(ShippingQuote, uuid.UUID(str(quote_id)))
    except ValueError:
        raise AppError("Unknown shipping quote.", 400) from None
    if quote is None or quote.buyer_id != buyer.id or quote.piece_id != piece.id:
        raise AppError("Shipping quote not found.", 404)
    if quote.status == QUOTE_PROCESSING:
        raise AppError("This shipping quote is still being priced. We'll let you know when it's ready.", 409)
    if quote.status not in BUYABLE_QUOTE_STATUSES:
        raise AppError("This shipping quote can't be used any more. Get a new one.", 409)
    if quote.expires_at and quote.expires_at <= _now():
        quote.status = QUOTE_EXPIRED
        db.commit()
        raise AppError("This shipping quote has expired. Get a new one.", 409)
    return quote


def quote_to_dict(quote: ShippingQuote) -> dict:
    priced = quote.price_incl_cents is not None and quote.status != QUOTE_PROCESSING
    return {
        "id": str(quote.id),
        "status": quote.status,
        "pending": quote.status == QUOTE_PROCESSING,
        "speed": quote.shipping_speed,
        "deliveryType": quote.delivery_type,
        "insurance": quote.insurance,
        "shippingCents": quote.shipping_cents if priced else None,
        "insuranceCents": (int(quote.insurance_cents or 0) if quote.insurance else 0) if priced else None,
        "totalShippingCents": quote.total_cents if priced else None,
        "currency": quote.currency,
        "reference": quote.provider_ref,
        "expiresAt": quote.expires_at.isoformat() if quote.expires_at else None,
        "createdAt": quote.created_at.isoformat() if quote.created_at else None,
    }


def pending_for(db: Session, provider_quote_id: str) -> Optional[ShippingQuote]:
    return db.execute(
        select(ShippingQuote).where(ShippingQuote.provider_quote_id == provider_quote_id)
    ).scalar_one_or_none()


# --- estimates (piece page, B19) ---------------------------------------------------------------

# Decision S-1: until Convelio prices US estimates, a range by billable weight — the larger of
# the real weight and the volumetric weight (L x W x H / 5000). Placeholder figures, to be
# tuned from real Convelio quotes; each row is (up to kg, low cents, high cents).
ESTIMATE_BANDS = (
    (5, 150_00, 350_00),
    (15, 250_00, 600_00),
    (40, 450_00, 1_000_00),
    (100, 900_00, 2_000_00),
    (None, 1_800_00, 4_500_00),
)


def billable_kg(piece: Piece) -> float:
    volumetric = (float(piece.package_length_cm) * float(piece.package_width_cm)
                  * float(piece.package_height_cm)) / 5000.0
    return max(float(piece.weight_kg), volumetric)


def table_estimate(piece: Piece) -> dict:
    kg = billable_kg(piece)
    for upto, low, high in ESTIMATE_BANDS:
        if upto is None or kg <= upto:
            return {"lowCents": low, "highCents": high}
    raise AssertionError("ESTIMATE_BANDS has no open-ended band")


def estimate(db: Session, piece: Piece, *, postcode: Optional[str], state: Optional[str]) -> dict:
    missing = convelio_mapper.missing_for_piece(piece)
    if missing:
        return {"available": False, "reason": "The artist hasn't added the packed size and weight yet.",
                "missing": missing}
    if shipping_config.enabled():
        artist = db.get(User, piece.user_id)
        try:
            body = convelio_mapper.estimate_body(piece=piece, pickup_address=pickup_address(db, artist),
                                                 postcode=postcode, state=state)
            result = convelio_client.estimate(body)
            fields = convelio_mapper.price_fields(result.get("price") or result)
            if fields.get("price_incl_cents"):
                cents = int(fields["price_incl_cents"])
                return {"available": True, "source": "convelio", "lowCents": cents,
                        "highCents": cents, "currency": "USD"}
        except AppError as exc:
            # A 422 is Convelio saying "no estimate for this route" — expected for US routes
            # today. Fall back to the table.
            logger.info("Convelio estimate unavailable for piece %s: %s", piece.id, exc.message)
    return {"available": True, "source": "table", "currency": "USD", **table_estimate(piece)}
