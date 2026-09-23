"""Plain factory functions for test data.

Functions rather than factory_boy: there are about eight models involved and explicit
keyword defaults read better than a DSL, especially for the money fields where the exact
numbers are the point of the test.

Every factory commits, because the code under test opens its own sessions and has to be
able to see the fixtures (see the `clean_db` docstring for why isolation works this way).
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from src.shared.config import commission as commission_policy
from src.shared.models.address import Address
from src.shared.models.auction import AUCTION_LIVE, HOLD_HELD, Auction, Hold
from src.shared.models.bid import BID_ACTIVE, Bid
from src.shared.models.order import Order, OrderItem
from src.shared.models.payout import Payout, PAYOUT_PENDING
from src.shared.models.piece import Piece
from src.shared.models.user import User


def _unique(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def make_user(
    db: Session,
    *,
    seller: bool = False,
    payouts_enabled: bool | None = None,
    stripe_account_id: str | None = None,
    onboarding_complete: bool = True,
    email_verified: bool = True,
    is_admin: bool = False,
    **overrides,
) -> User:
    """A fully onboarded user by default — most tests care about money, not signup state.

    `seller=True` also enables Stripe payouts and sets a Connect account id unless told
    otherwise, since a seller without those is blocked from payout and that is its own test.
    """
    if payouts_enabled is None:
        payouts_enabled = seller
    if stripe_account_id is None and seller:
        stripe_account_id = _unique("acct_")
    username = overrides.pop("username", None) or _unique("user_")
    name = overrides.pop("name", None) or "Test User"

    user = User(
        id=uuid.uuid4(),
        username=username,
        email=f"{_unique('u')}@example.test",
        name=name,
        email_verified=email_verified,
        onboarding_complete=onboarding_complete,
        seller_enabled=seller,
        stripe_account_id=stripe_account_id,
        stripe_payouts_enabled=payouts_enabled,
        is_admin=is_admin,
        **overrides,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_piece(
    db: Session,
    owner: User,
    *,
    price_cents: int = 100_00,
    listing_type: str = "fixed",
    status: str = "live",
    is_for_sale: bool = True,
    **overrides,
) -> Piece:
    """A live, for-sale fixed-price piece by default.

    For an auction, `price_cents` is the STARTING BID, not a buy price — the same column
    carries both, which is precisely why the fixed/auction guard matters.
    """
    # medium and dimensions are required of any for-sale listing, so a factory that omitted
    # them produced pieces the application would refuse to create.
    overrides.setdefault("medium", "Oil on canvas")
    overrides.setdefault("dimensions", {"width": 40, "height": 60, "unit": "cm"})

    piece = Piece(
        id=uuid.uuid4(),
        user_id=owner.id,
        title="Test Piece",
        media_url="https://example.test/piece.jpg",
        media_type="image",
        is_for_sale=is_for_sale,
        listing_type=listing_type if is_for_sale else None,
        price_cents=price_cents,
        status=status,
        **overrides,
    )
    db.add(piece)
    db.commit()
    db.refresh(piece)
    return piece


def make_auction(
    db: Session,
    piece: Piece,
    seller: User,
    *,
    starting_bid_cents: int | None = None,
    duration_days: int = 7,
    reserve_cents: int | None = None,
    status: str = AUCTION_LIVE,
    opens_at: datetime | None = None,
    closes_at: datetime | None = None,
    soft_close_enabled: bool = True,
    event_id=None,
    delivery_mode: str | None = None,
) -> Auction:
    """A running auction by default, opened now and closing in `duration_days`."""
    now = datetime.now(timezone.utc)
    auction = Auction(
        id=uuid.uuid4(),
        piece_id=piece.id,
        seller_id=seller.id,
        status=status,
        starting_bid_cents=starting_bid_cents or piece.price_cents or 100_00,
        reserve_cents=reserve_cents,
        duration_days=None if event_id else duration_days,
        opens_at=opens_at or now,
        closes_at=closes_at or (now + timedelta(days=duration_days)),
        soft_close_enabled=soft_close_enabled,
        event_id=event_id,
        delivery_mode=delivery_mode,
        commission_bps=commission_policy.resolve_bps(seller, commission_policy.SALE_ART),
    )
    db.add(auction)
    db.commit()
    db.refresh(auction)
    return auction


def make_bid(
    db: Session,
    auction: Auction,
    bidder: User,
    amount_cents: int,
    *,
    with_hold: bool = True,
    hold_status: str = HOLD_HELD,
) -> Bid:
    """A bid, with the money behind it by default.

    `with_hold=False` exists only for testing the path where a bid somehow has no hold,
    which the closer has to survive rather than crash on.
    """
    bid = Bid(
        id=uuid.uuid4(),
        auction_id=auction.id,
        bidder_id=bidder.id,
        amount_cents=amount_cents,
        status=BID_ACTIVE,
    )
    db.add(bid)
    db.flush()
    if with_hold:
        db.add(
            Hold(
                id=uuid.uuid4(),
                bid_id=bid.id,
                bidder_id=bidder.id,
                amount_cents=amount_cents,
                status=hold_status,
                stripe_payment_intent_id=f"pi_test_{uuid.uuid4().hex[:16]}",
                stripe_payment_method_id="pm_test_card",
                capture_before=datetime.now(timezone.utc) + timedelta(days=7),
            )
        )
    db.commit()
    db.refresh(bid)
    return bid


def make_order(
    db: Session,
    *,
    buyer: User,
    seller: User,
    piece: Piece | None = None,
    artwork_cents: int = 100_00,
    shipping_cents: int = 5_00,
    tax_cents: int = 0,
    status: str = "pending_payment",
    payment_reference: str | None = None,
    stripe_charge_id: str | None = None,
    commission_bps: int | None = None,
    **overrides,
) -> Order:
    """An order whose components sum to its total.

    They are summed here rather than passed in because nothing in production enforces
    `total == artwork + shipping + tax`, and a factory that let them drift would produce
    ledger imbalances unrelated to whatever the test is actually checking.
    """
    order = Order(
        id=uuid.uuid4(),
        buyer_id=buyer.id,
        seller_id=seller.id,
        status=status,
        shipping_method="standard",
        shipping_address_snapshot={"line1": "1 Test St", "city": "Dallas", "country": "US"},
        artwork_cents=artwork_cents,
        shipping_cents=shipping_cents,
        tax_cents=tax_cents,
        total_cents=artwork_cents + shipping_cents + tax_cents,
        # Resolved the same way checkout does, so a test seller on the reduced tier gets
        # the reduced rate without the test having to know the number.
        commission_bps=commission_bps
        if commission_bps is not None
        else commission_policy.resolve_bps(seller, commission_policy.SALE_ART),
        payment_provider="stripe" if payment_reference else None,
        payment_reference=payment_reference,
        stripe_charge_id=stripe_charge_id,
        **overrides,
    )
    db.add(order)
    db.flush()
    if piece is not None:
        db.add(
            OrderItem(
                id=uuid.uuid4(),
                order_id=order.id,
                piece_id=piece.id,
                price_cents=artwork_cents,
            )
        )
        # Keep the piece consistent with the order, as real checkout does: create_order
        # reserves it, and the payment webhook then moves reserved -> sold. Leaving it
        # `live` under a paid order is a state the application can never produce, and tests
        # built on it would be asserting against fiction.
        piece.status = _piece_status_for(status)
    db.commit()
    db.refresh(order)
    return order


# Set directly rather than through piece_state: a factory builds a finished state, it does
# not replay the transitions that would have produced it.
_PIECE_STATUS_BY_ORDER_STATUS = {
    "pending_payment": "reserved",
    "paid": "sold",
    "shipped": "sold",
    "awaiting_confirmation": "sold",
    "completed": "sold",
    "disputed": "sold",
}


def _piece_status_for(order_status: str) -> str:
    """Where the piece sits while its order is in `order_status`."""
    return _PIECE_STATUS_BY_ORDER_STATUS.get(order_status, "live")


def make_payout(db: Session, order: Order, *, status: str = PAYOUT_PENDING, **overrides) -> Payout:
    payout = Payout(
        id=uuid.uuid4(),
        order_id=order.id,
        seller_id=order.seller_id,
        status=status,
        idempotency_key=f"payout:{order.id}",
        **overrides,
    )
    db.add(payout)
    db.commit()
    db.refresh(payout)
    return payout


def make_address(db: Session, user: User, **overrides) -> Address:
    address = Address(
        id=uuid.uuid4(),
        user_id=user.id,
        first_name="Test",
        last_name="Collector",
        phone="+15555550123",
        line1="1 Test St",
        city="Dallas",
        state="TX",
        zip="75201",
        country="US",
        **overrides,
    )
    db.add(address)
    db.commit()
    db.refresh(address)
    return address


def make_event(
    db: Session,
    host: User,
    *,
    title: str = "Test Event",
    status: str = "draft",
    starts_in: timedelta = timedelta(days=7),
    duration: timedelta = timedelta(hours=3),
    category: str | None = "gallery_walk",
    capacity: int | None = None,
    **overrides,
):
    """An event a week out, three hours long — long enough that an event auction's close
    (30 minutes before the end) still lands after its open."""
    from src.shared.models.event import Event

    now = datetime.now(timezone.utc)
    starts_at = overrides.pop("starts_at", now + starts_in)
    event = Event(
        id=uuid.uuid4(),
        host_id=host.id,
        title=title,
        status=status,
        starts_at=starts_at,
        ends_at=overrides.pop("ends_at", starts_at + duration),
        category=category,
        capacity=capacity,
        timezone="America/Chicago",
        venue_name="Cedars Union",
        **overrides,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def make_event_participant(db: Session, event, user: User, *, role: str = "artist"):
    from src.shared.models.event import EventParticipant

    row = EventParticipant(id=uuid.uuid4(), event_id=event.id, user_id=user.id, role=role)
    db.add(row)
    db.commit()
    return row
