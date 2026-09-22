"""Turning the admin DAO's rows into JSON.

`admin_dao` returns ORM objects because it was written for Jinja, which reads attributes off
them happily. JSON cannot, so the shaping lives here rather than in the DAO — changing the
DAO would mean rewriting the HTML console at the same time, and the two consoles are meant to
stay on one source of truth.

Everything is explicit. A generic "serialise every column" helper would quietly publish
whatever column somebody adds next, and these rows carry shipping addresses and payment
references.
"""
from datetime import date, datetime
from decimal import Decimal
import uuid


def _v(value):
    """Scalars JSON has no opinion about."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return int(value)
    return value


def _fields(obj, *names) -> dict:
    """Pick named attributes off an ORM object, camelCasing the keys."""
    if obj is None:
        return None
    out = {}
    for name in names:
        parts = name.split("_")
        key = parts[0] + "".join(p.title() for p in parts[1:])
        out[key] = _v(getattr(obj, name, None))
    return out


def person(user) -> dict | None:
    return _fields(user, "id", "username", "name", "email")


def order_row(order) -> dict | None:
    """A row in the orders queue: enough to triage, not the whole record."""
    return _fields(
        order, "id", "status", "total_cents", "artwork_cents", "shipping_cents", "tax_cents",
        "prepaid_cents", "shipping_method", "payment_reference", "stripe_charge_id",
        "buyer_id", "seller_id", "created_at", "updated_at",
    )


def order_full(detail: dict) -> dict:
    """The order detail screen. Includes the address, which is why nothing here is generic."""
    order = detail["order"]
    return {
        "order": order_row(order),
        # Read by ops when a courier needs re-booking, so it has to be here — and is the
        # reason this module never serialises by iterating columns.
        "shippingAddress": order.shipping_address_snapshot,
        "buyer": person(detail.get("buyer")),
        "seller": person(detail.get("seller")),
        "pieces": [
            _fields(p, "id", "title", "status", "price_cents", "image_url")
            for p in detail.get("pieces") or []
        ],
        "shipment": _fields(
            detail.get("shipment"), "id", "courier", "tracking_number", "status",
            "actual_cost_cents", "created_at", "updated_at",
        ),
        "payout": _fields(
            detail.get("payout"), "id", "status", "amount_cents", "stripe_transfer_id",
            "failure_reason", "created_at", "updated_at",
        ),
        "dispute": _fields(
            detail.get("dispute"), "id", "status", "reason", "amount_cents", "created_at",
        ),
        "ledger": [
            {**entry, "createdAt": _v(entry.get("createdAt") or entry.get("created_at"))}
            for entry in (_ledger(detail.get("ledger")))
        ],
    }


def _ledger(rows) -> list[dict]:
    out = []
    for row in rows or []:
        out.append({
            "transactionType": row.get("transactionType"),
            "account": row.get("account"),
            "direction": row.get("direction"),
            "amountCents": row.get("amountCents"),
            "createdAt": _v(row.get("createdAt")),
        })
    return out


def dispute_row(row: dict) -> dict:
    return {
        "dispute": _fields(row["dispute"], "id", "status", "reason", "amount_cents",
                           "stripe_dispute_id", "created_at"),
        "order": order_row(row["order"]),
    }


def failed_payout_row(row: dict) -> dict:
    return {
        "payout": _fields(row["payout"], "id", "status", "amount_cents", "failure_reason",
                          "stripe_transfer_id", "updated_at"),
        "order": order_row(row["order"]),
        "seller": person(row["seller"]),
    }


def auction_row(row: dict) -> dict:
    return {
        "auction": _fields(row["auction"], "id", "status", "starting_bid_cents",
                           "auction_ends_at", "winning_bid_id", "winner_deadline_at",
                           "cascade_depth", "event_id"),
        "piece": _fields(row["piece"], "id", "title", "status", "price_cents", "image_url"),
        "seller": person(row["seller"]),
        "bidCount": row.get("bidCount"),
        "highBidCents": row.get("highBidCents"),
    }


def event_row(row: dict) -> dict:
    return {
        "event": _fields(row["event"], "id", "title", "status", "category", "starts_at",
                         "ends_at", "venue_name", "capacity"),
        "host": person(row["host"]),
        "sellingCount": row.get("sellingCount"),
        "goingCount": row.get("goingCount"),
    }
