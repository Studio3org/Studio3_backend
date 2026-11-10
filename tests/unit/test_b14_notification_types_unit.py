"""Separate buyer and seller types, shipping types and event reminders (BACKEND_CHANGES B14).

Runs without a database (`pytest tests/unit/test_b14_notification_types.py --noconftest`);
the same flows against Postgres are in tests/api/test_b14_notification_types.py.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from src.modules.events import reminders
from src.modules.orders import order_notices
from src.modules.shipments import shipments_controller

BUYER, SELLER, HOST = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def create_and_push(_db, **kw):
        calls.append(kw)

    from src.modules.notifications import notifications_dao

    monkeypatch.setattr(notifications_dao, "create_and_push", create_and_push)
    return calls


class _Db:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.rollbacks = 0

    def get(self, _model, key):
        return self.rows.get(key)

    def rollback(self):
        self.rollbacks += 1


# --- buyer and seller -------------------------------------------------------------------------

def test_the_seller_hears_sale_and_the_buyer_purchase(monkeypatch, sent):
    piece_id = uuid.uuid4()
    order = SimpleNamespace(id=uuid.uuid4(), buyer_id=BUYER, seller_id=SELLER, total_cents=95000)
    monkeypatch.setattr(
        order_notices.orders_dao, "list_items", lambda _db, _id: [SimpleNamespace(piece_id=piece_id)]
    )
    db = _Db({BUYER: SimpleNamespace(name="Jun Park"), piece_id: SimpleNamespace(title="Tide")})

    order_notices.notify_paid(db, order)

    seller, buyer = sent
    assert (seller["user_id"], seller["type"], seller["actor_id"]) == (SELLER, "sale", BUYER)
    assert seller["payload"] == {"pieceTitle": "Tide", "totalCents": 95000}
    assert seller["body"] == "Jun Park purchased 'Tide'"
    assert (buyer["user_id"], buyer["type"], buyer["actor_id"]) == (BUYER, "purchase", SELLER)
    assert buyer["body"] == "Your order for 'Tide' is confirmed"
    assert seller["target_id"] == buyer["target_id"] == order.id


# --- shipping -----------------------------------------------------------------------------------

def test_shipping_has_its_own_types_and_says_which_side(monkeypatch, sent):
    monkeypatch.setattr(shipments_controller, "_piece_title", lambda _db, _order: "Tide")
    order = SimpleNamespace(id=uuid.uuid4(), buyer_id=BUYER, seller_id=SELLER)
    shipment = SimpleNamespace(courier="UPS", tracking_number="1Z999")

    shipments_controller._notify_shipped(None, order, shipment)
    shipments_controller._notify_delivered(None, order)

    to_buyer, to_seller, delivered = sent
    assert [n["type"] for n in sent] == ["order_shipped", "order_shipped", "order_delivered"]
    assert (to_buyer["user_id"], to_buyer["payload"]["side"]) == (BUYER, "buyer")
    assert (to_seller["user_id"], to_seller["payload"]["side"]) == (SELLER, "seller")
    assert to_buyer["payload"]["trackingNumber"] == "1Z999"
    assert (delivered["user_id"], delivered["payload"]["side"]) == (BUYER, "buyer")


# --- event reminders: what it says ------------------------------------------------------------

NOW = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)  # 1 PM in Chicago


def _event(**kw):
    base = dict(
        id=uuid.uuid4(), host_id=HOST, title="Night Market", venue_name="Cedars Union",
        timezone="America/Chicago", starts_at=datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc),
    )
    return SimpleNamespace(**{**base, **kw})


def test_the_reminder_says_when_in_the_events_own_time():
    # 01:00 UTC on the 2nd is 7 PM on the 1st in Chicago: today, not tomorrow.
    assert reminders.reminder_message(_event(), NOW) == "Night Market is today at 7 PM at Cedars Union."
    tomorrow = _event(starts_at=datetime(2026, 11, 2, 16, 30, tzinfo=timezone.utc))
    assert reminders.reminder_message(tomorrow, NOW) == (
        "Night Market is tomorrow at 10:30 AM at Cedars Union."
    )
    assert reminders.reminder_message(_event(venue_name=None), NOW) == "Night Market is today at 7 PM."


def test_without_a_time_zone_it_says_how_far_away():
    assert reminders.reminder_message(_event(timezone=None), NOW) == (
        "Night Market starts in 7 hours at Cedars Union."
    )
    assert reminders.reminder_message(_event(timezone="Not/AZone"), NOW).startswith(
        "Night Market starts in 7 hours"
    )


def test_clock():
    at = lambda h, m=0: datetime(2026, 1, 1, h, m)  # noqa: E731
    assert [reminders._clock(at(0)), reminders._clock(at(9, 5)), reminders._clock(at(12)),
            reminders._clock(at(23, 45))] == ["12 AM", "9:05 AM", "12 PM", "11:45 PM"]


# --- event reminders: who and when --------------------------------------------------------------

def _sql(statement):
    return str(statement.compile(dialect=postgresql.dialect()))


def test_due_events_are_published_and_between_an_hour_and_a_day_away():
    query = reminders.due_events_query(NOW)
    sql = _sql(query)
    assert "events.status = " in sql
    assert "events.starts_at > " in sql and "events.starts_at <= " in sql
    params = query.compile(dialect=postgresql.dialect()).params
    bounds = sorted(v for v in params.values() if isinstance(v, datetime))
    # The day-before window starts where the 2-hour reminder's ends (ticketing spec).
    assert bounds == [NOW + timedelta(hours=2), NOW + timedelta(hours=24)]
    soon = reminders.due_events_query(NOW, soon=True).compile(dialect=postgresql.dialect()).params
    assert sorted(v for v in soon.values() if isinstance(v, datetime)) == [
        NOW + timedelta(minutes=10), NOW + timedelta(hours=2)]
    assert "published" in params.values()


class _Capture:
    def __init__(self, user_ids):
        self.statement = None
        self.user_ids = user_ids
        self.commits = 0

    def execute(self, statement):
        self.statement = statement
        return SimpleNamespace(scalars=lambda: iter(self.user_ids))

    def commit(self):
        self.commits += 1


def test_claiming_marks_only_people_going_and_not_yet_reminded():
    someone = uuid.uuid4()
    db = _Capture([someone])
    assert reminders.claim(db, uuid.uuid4(), NOW) == [someone]
    sql = _sql(db.statement)
    assert sql.startswith("UPDATE event_rsvps SET ") and "reminded_at=" in sql
    assert "event_rsvps.status = " in sql and "event_rsvps.reminded_at IS NULL" in sql
    assert "RETURNING event_rsvps.user_id" in sql
    assert db.commits == 1


def test_remind_skips_deleted_accounts_and_survives_a_failure(monkeypatch):
    alive, gone, broken = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    calls = []

    def create_and_push(_db, **kw):
        if kw["user_id"] == broken:
            raise RuntimeError("push failed")
        calls.append(kw)

    from src.modules.notifications import notifications_dao

    monkeypatch.setattr(notifications_dao, "create_and_push", create_and_push)
    db = _Db({
        alive: SimpleNamespace(deleted_at=None),
        gone: SimpleNamespace(deleted_at=NOW),
        broken: SimpleNamespace(deleted_at=None),
    })
    event = _event()

    assert reminders.remind(db, event, [broken, gone, alive], NOW) == 1
    (call,) = calls
    assert call["user_id"] == alive
    assert call["type"] == "event_reminder"
    assert (call["actor_id"], call["target_type"], call["target_id"]) == (HOST, "event", event.id)
    assert call["body"] == "Night Market is today at 7 PM at Cedars Union."
    assert db.rollbacks == 1


def test_a_run_reminds_each_due_event_once(monkeypatch):
    first, second = _event(), _event(title="Open Studio")
    db = SimpleNamespace(execute=lambda _q: SimpleNamespace(scalars=lambda: iter([first, second])))
    monkeypatch.setattr(reminders, "_email", lambda *_a: None)
    claimed = {first.id: [uuid.uuid4(), uuid.uuid4()], second.id: []}
    monkeypatch.setattr(reminders, "claim",
                        lambda _db, event_id, _now, soon=False: [] if soon else claimed[event_id])
    reminded = []
    monkeypatch.setattr(
        reminders, "remind",
        lambda _db, event, ids, _now: reminded.append((event.title, len(ids))) or len(ids),
    )
    assert reminders.send_event_reminders(db, NOW) == 2
    # Nobody left to claim for the second event: it is not sent to.
    assert reminded == [("Night Market", 2)]
