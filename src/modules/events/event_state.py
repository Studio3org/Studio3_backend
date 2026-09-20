"""The single writer of `event.status`.

Same shape as piece_state and orders_dao.transition_order, for the same reason: a status
that any caller can assign is a status nobody can reason about. Publishing an event creates
real listings and cancelling one refunds real bidders, so the set of legal moves is written
down once, here.

`published -> draft` is deliberately absent. Once an event is out, people have saved it and
work has been listed against it; quietly pulling it back to a draft would leave those
listings live with nothing pointing at them. Withdrawing is `cancelled`, which has a
defined cleanup.
"""
from typing import Optional

from sqlalchemy.orm import Session

from src.shared.models.event import (
    EVENT_ARCHIVED,
    EVENT_CANCELLED,
    EVENT_DRAFT,
    EVENT_PUBLISHED,
    EVENT_STATUSES,
    Event,
)
from src.shared.utils.app_error import AppError
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

VALID_TRANSITIONS: dict[str, set[str]] = {
    EVENT_DRAFT: {EVENT_PUBLISHED, EVENT_CANCELLED},
    # Archiving is what the nightly sweep does once an event is over.
    EVENT_PUBLISHED: {EVENT_CANCELLED, EVENT_ARCHIVED},
    EVENT_CANCELLED: set(),
    EVENT_ARCHIVED: set(),
}

# Nothing moves out of these.
TERMINAL_STATUSES = tuple(
    status for status, allowed in VALID_TRANSITIONS.items() if not allowed
)


def transition_event(
    db: Session,
    event: Event,
    new_status: str,
    *,
    allowed_from: Optional[set] = None,
    reason: Optional[str] = None,
    commit: bool = True,
) -> Event:
    """Move an event to `new_status`, enforcing VALID_TRANSITIONS.

    Same-status is an idempotent no-op so a re-run archive sweep is harmless. `commit=False`
    is for callers batching this with other writes — publishing an event also creates the
    lineup's listings, and those have to land together or not at all.
    """
    current = event.status
    if current == new_status:
        return event

    if new_status not in EVENT_STATUSES:
        raise AppError(f"Unknown event status '{new_status}'.", 400)
    if allowed_from is not None and current not in allowed_from:
        raise AppError(f"Cannot move this event from '{current}' to '{new_status}'.", 409)
    if new_status not in VALID_TRANSITIONS.get(current, set()):
        raise AppError(f"Cannot move this event from '{current}' to '{new_status}'.", 409)

    event.status = new_status
    if reason:
        logger.info("Event %s: %s -> %s (%s)", event.id, current, new_status, reason)

    if commit:
        db.commit()
        db.refresh(event)
    return event
