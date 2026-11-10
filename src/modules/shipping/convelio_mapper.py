"""Our pieces, addresses and people as Convelio's request shapes (the v2 OpenAPI spec).

Our own checks run first, because Convelio reports validation errors one at a time and in
technical terms (the sandbox showed it). A piece missing what Convelio needs — packed size,
weight, a pickup address — is refused here with a message naming what's missing.

Sizes are stored in cm and kg and sent metric, rounded up to whole units (Convelio takes
integers). Money is integer cents in USD, like Stripe and our *_cents columns.
"""
from __future__ import annotations

import math
import re
from typing import Optional

from src.shared.utils.app_error import AppError

ITEM_TYPES = ("fine_art.painting", "fine_art.picture", "fine_art.sculpture", "fine_art.installation",
              "decorative.ceramics", "decorative.artefact", "other.other")
MATERIALS = ("glass", "stone", "ceramic", "plaster_concrete", "resin", "none", "ornate_frame",
             "framed_glass", "unframed", "lacquered", "stackable", "dismountable", "can_be_rolled",
             "can_be_folded", "horizontal", "pastel", "protruding", "wet", "rolled_in_plastic")
PACKINGS = ("not_packed", "wood_crated")


class ShippingNotReady(AppError):
    """The piece or an address is missing something Convelio needs. 409 with the list."""

    def __init__(self, message: str, missing: list[str]):
        super().__init__(message, 409)
        self.missing = missing


def _ceil(value) -> int:
    return max(1, int(math.ceil(float(value))))


def phone_e164(raw: Optional[str]) -> Optional[str]:
    """US numbers to +1XXXXXXXXXX; anything already international is kept."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def guess_item_type(piece) -> str:
    if getattr(piece, "shipping_item_type", None) in ITEM_TYPES:
        return piece.shipping_item_type
    text = " ".join(str(getattr(piece, f, "") or "") for f in ("medium", "title")).lower()
    if any(w in text for w in ("sculpt", "bronze", "marble", "statue")):
        return "fine_art.sculpture"
    if any(w in text for w in ("ceramic", "porcelain", "pottery", "vase")):
        return "decorative.ceramics"
    if any(w in text for w in ("photo", "print", "drawing", "paper")):
        return "fine_art.picture"
    if "install" in text:
        return "fine_art.installation"
    return "fine_art.painting"


def guess_materials(piece) -> list[str]:
    chosen = [m for m in (getattr(piece, "shipping_materials", None) or []) if m in MATERIALS]
    if chosen:
        return chosen
    framing = str(getattr(piece, "framing_mounting", "") or "").lower()
    if "glass" in framing or "glazed" in framing:
        return ["framed_glass"]
    if "unframed" in framing or not framing:
        return ["unframed"]
    return ["none"]


def missing_for_piece(piece) -> list[str]:
    missing = []
    for attr, label in (("package_length_cm", "packed length"), ("package_width_cm", "packed width"),
                        ("package_height_cm", "packed height"), ("weight_kg", "weight")):
        if not getattr(piece, attr, None):
            missing.append(label)
    if not (getattr(piece, "declared_value_cents", None) or getattr(piece, "price_cents", None)):
        missing.append("value")
    return missing


def item(piece, value_cents: Optional[int] = None) -> dict:
    missing = missing_for_piece(piece)
    if missing:
        raise ShippingNotReady("The artist needs to add this piece's " + ", ".join(missing)
                               + " before it can ship.", missing)
    title = (piece.title or "Artwork").strip()
    description = title
    if getattr(piece, "medium", None):
        description = f"{title} — {piece.medium}"
    packing = piece.shipping_packing if getattr(piece, "shipping_packing", None) in PACKINGS else "not_packed"
    return {
        "name": title[:255],
        "description": description[:800],
        "quantity": 1,
        "current_packing": packing,
        "measurement_system": "metric",
        "type": guess_item_type(piece),
        "materials": guess_materials(piece),
        "value": {"amount": int(value_cents or piece.declared_value_cents or piece.price_cents),
                  "currency_code": "USD"},
        "length": _ceil(piece.package_length_cm),
        "width": _ceil(piece.package_width_cm),
        "height": _ceil(piece.package_height_cm),
        "weight": _ceil(piece.weight_kg),
    }


def address(addr, *, who: str) -> dict:
    """US only at launch (D-11)."""
    if addr is None:
        raise ShippingNotReady(f"{who} needs an address.", ["address"])
    country = (addr.country or "").upper()
    if country != "US":
        raise AppError("Shipping is US only for now.", 400)
    street = ", ".join(part for part in (addr.line1, getattr(addr, "line2", None)) if part)
    return {"street": street[:255], "city": addr.city, "state": addr.state,
            "postcode": addr.zip, "country_code": "US"}


def contact(addr, email: Optional[str], *, who: str) -> dict:
    phone = phone_e164(getattr(addr, "phone", None))
    missing = [label for label, value in (("first name", addr.first_name), ("last name", addr.last_name),
                                          ("email", email), ("phone", phone)) if not value]
    if missing:
        raise ShippingNotReady(f"{who}'s contact details need: " + ", ".join(missing) + ".", missing)
    return {"first_name": addr.first_name, "last_name": addr.last_name, "email": email, "phone": phone}


def quote_body(*, piece, artist, pickup_address, buyer, delivery_address, speed: str,
               delivery_type: str, insurance: bool, reference: str) -> dict:
    missing = missing_for_piece(piece) + ([] if pickup_address is not None else ["pickup address"])
    if missing:
        raise ShippingNotReady("The artist needs to add this piece's " + ", ".join(missing)
                               + " before it can ship.", missing)
    return {
        "delivery": {
            "type": delivery_type,
            "address": address(delivery_address, who="The delivery"),
            "contact": contact(delivery_address, buyer.email, who="The buyer"),
        },
        "shipping_speed": speed,
        "contract_insurance": bool(insurance),
        "pickups": [{
            "address": address(pickup_address, who="The pickup"),
            "contacts": [contact(pickup_address, artist.email, who="The artist")],
            "items": [item(piece)],
        }],
        "customer_email": buyer.email,
        "customer_reference_number": reference[:255],
    }


def estimate_body(*, piece, pickup_address, postcode: Optional[str], state: Optional[str]) -> dict:
    """An estimate needs no contacts: a piece, where it is and roughly where it's going."""
    if pickup_address is None:
        raise ShippingNotReady("The artist hasn't added a pickup address yet.", ["pickup address"])
    destination = {"country_code": "US"}
    if postcode:
        destination["postcode"] = postcode
    if state:
        destination["state"] = state
    return {
        "delivery": {"type": "curbside", "address": destination},
        "shipping_speed": "regular_speed",
        "pickups": [{"address": address(pickup_address, who="The pickup"), "items": [item(piece)]}],
    }


def price_fields(price: Optional[dict]) -> dict:
    price = price or {}
    excl = price.get("vat_excluded_amount")
    incl = price.get("vat_included_amount", excl)
    vat = price.get("vat_amount")
    if vat is None and excl is not None and incl is not None:
        vat = incl - excl
    return {"price_excl_cents": excl, "vat_cents": vat, "price_incl_cents": incl,
            "insurance_cents": price.get("insurance_amount") or 0}
