"""Ticket stock under real concurrency: two buyers, one ticket left.

The guard is the row lock on the ticket type taken in ticketing_service._priced. Real threads
with their own database sessions, so Postgres has to serialise them; without the lock both
would read "1 left" and both be sold it.
"""
import time

import pytest

from src.modules.events import ticketing_service
from src.shared.config.database import SessionLocal
from src.shared.models.event import Event
from src.shared.models.ticketing import LIVE_TICKET_STATUSES, Ticket, TicketType
from src.shared.models.user import User
from src.shared.utils.app_error import AppError
from tests.factories import make_event, make_user
from tests.helpers import run_concurrently


@pytest.mark.slow
def test_two_buyers_cannot_both_get_the_last_ticket(db, monkeypatch):
    host = make_user(db, seller=True)
    event = make_event(db, host, status="published")
    last = TicketType(event_id=event.id, name="GA", price_cents=0, quantity=1, sort_order=0)
    db.add(last)
    db.commit()
    buyers = [make_user(db).id, make_user(db).id]

    # Widen the race: pause right after counting stock, where an unlocked version would let
    # both buyers read "1 left" before either writes. With the lock, the second buyer waits
    # at the lock and counts after the first has committed.
    counted = ticketing_service._taken

    def slow_count(*args, **kwargs):
        result = counted(*args, **kwargs)
        time.sleep(0.3)
        return result

    monkeypatch.setattr(ticketing_service, "_taken", slow_count)

    def buy(buyer_id):
        def run():
            session = SessionLocal()
            try:
                return ticketing_service.place_order(
                    session, session.get(Event, event.id), session.get(User, buyer_id),
                    [{"ticketTypeId": str(last.id), "quantity": 1}], None,
                )
            finally:
                session.close()
        return run

    results = run_concurrently(buy(buyers[0]), buy(buyers[1]))

    wins = [r for r in results if isinstance(r, dict)]
    refusals = [r for r in results if isinstance(r, AppError)]
    assert len(wins) == 1 and len(refusals) == 1, results
    assert refusals[0].status_code == 409
    db.expire_all()
    assert db.query(Ticket).filter(
        Ticket.ticket_type_id == last.id, Ticket.status.in_(LIVE_TICKET_STATUSES)
    ).count() == 1
