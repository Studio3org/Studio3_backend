"""Telling the two sides of an order that it was paid for.

The buyer's copy is `purchase` ("Order confirmed") and the seller's is `sale` ("You made a
sale!"), so each side's switch in Settings turns off only their own (BACKEND_CHANGES B14):
the buyer's Orders and shipping, the seller's Bids and sales. Both used to be `purchase`, so
a seller who muted their receipts also muted being told they had sold something.
"""
from sqlalchemy.orm import Session

from src.modules.notifications import notifications_dao
from src.modules.orders import orders_dao
from src.shared.models.order import Order
from src.shared.models.piece import Piece
from src.shared.models.user import User

PURCHASE = "purchase"
SALE = "sale"


def notify_paid(db: Session, order: Order) -> None:
    """Call after the commit that marks the order paid. Raises what `create_and_push` raises;
    callers that must not fail on a notification catch it."""
    buyer = db.get(User, order.buyer_id)
    items = orders_dao.list_items(db, order.id)
    piece = db.get(Piece, items[0].piece_id) if items else None
    title = piece.title if piece else None
    buyer_name = buyer.name if buyer else "Someone"
    notifications_dao.create_and_push(
        db,
        user_id=order.seller_id,
        type=SALE,
        actor_id=order.buyer_id,
        target_type="order",
        target_id=order.id,
        payload={"pieceTitle": title, "totalCents": order.total_cents},
        title="You made a sale!",
        body=f"{buyer_name} purchased '{title}'" if title else f"{buyer_name} completed a purchase",
    )
    notifications_dao.create_and_push(
        db,
        user_id=order.buyer_id,
        type=PURCHASE,
        actor_id=order.seller_id,
        target_type="order",
        target_id=order.id,
        payload={"pieceTitle": title},
        title="Order confirmed",
        body=f"Your order for '{title}' is confirmed" if title else "Your order is confirmed",
    )
