"""Writing the audit trail.

One function, `record`, and a deliberate rule about how it fails: **it never raises.** An
audit write that broke a refund would be worse than a missing audit row — the operator would
retry, and a retry of a refund is a second refund. So a failure here is logged and swallowed,
and the action it was describing still completes.

That is a real trade-off and worth naming: it means the trail can have holes. The alternative
— letting bookkeeping veto the thing it is bookkeeping — is worse.

`record` is called *after* the action it describes has committed. Recording an intention that
then failed would produce a log of things that did not happen, which is more misleading than
no log at all.
"""
import uuid
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from src.shared.models.audit import ACTOR_ADMIN, ACTOR_SYSTEM, AuditEvent
from src.shared.models.user import User
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)


def record(
    db: Session,
    action: str,
    *,
    actor: Optional[User] = None,
    actor_type: str = ACTOR_ADMIN,
    subject_type: Optional[str] = None,
    subject_id: Optional[uuid.UUID] = None,
    detail: Optional[dict] = None,
    note: Optional[str] = None,
    commit: bool = True,
) -> Optional[AuditEvent]:
    """Record that something happened. Returns None if it could not be written.

    Call after the action has committed — see the module docstring for why.
    """
    try:
        event = AuditEvent(
            id=uuid.uuid4(),
            action=action,
            actor_id=actor.id if actor else None,
            actor_type=actor_type,
            # Captured now, so the row still reads sensibly after the account is renamed or
            # removed.
            actor_label=_label(actor, actor_type),
            subject_type=subject_type,
            subject_id=subject_id,
            detail=_clean(detail),
            note=(note or None) and note[:2000],
        )
        db.add(event)
        if commit:
            db.commit()
        else:
            db.flush()
        return event
    except Exception:
        # Never let bookkeeping veto the thing it is bookkeeping. A retry of a refund is a
        # second refund.
        logger.exception("Audit write failed for %s on %s %s", action, subject_type, subject_id)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def system(db: Session, action: str, **kwargs) -> Optional[AuditEvent]:
    """A scheduled job acting on its own.

    Worth distinguishing in a list: "the sweep cancelled this auction" and "a person
    cancelled this auction" are different events, and only one of them has somebody to ask
    about it.
    """
    kwargs.pop("actor", None)
    return record(db, action, actor=None, actor_type=ACTOR_SYSTEM, **kwargs)


def for_subject(
    db: Session, subject_type: str, subject_id: uuid.UUID, limit: int = 50
) -> list[AuditEvent]:
    """Everything that happened to one thing, most recent first."""
    return list(
        db.execute(
            select(AuditEvent)
            .where(
                AuditEvent.subject_type == subject_type,
                AuditEvent.subject_id == subject_id,
            )
            .order_by(desc(AuditEvent.created_at))
            .limit(limit)
        ).scalars()
    )


def recent(
    db: Session,
    *,
    action: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AuditEvent]:
    stmt = select(AuditEvent)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    return list(
        db.execute(
            stmt.order_by(desc(AuditEvent.created_at)).limit(limit).offset(max(offset, 0))
        ).scalars()
    )


def _label(actor: Optional[User], actor_type: str) -> Optional[str]:
    if actor is None:
        return "system" if actor_type == ACTOR_SYSTEM else None
    name = (actor.name or "").strip()
    handle = (actor.username or "").strip()
    if name and handle:
        return f"{name} (@{handle})"
    return name or (f"@{handle}" if handle else None)


def _clean(detail: Optional[dict]) -> Optional[dict]:
    """Keep the detail small and free of anything that would make this a second copy of
    somebody's personal information.

    An audit row says what was done and to what. A shipping address or a card is not part of
    that, and copying one here just doubles the number of places it has to be protected.
    """
    if not detail:
        return None
    blocked = {"address", "shipping_address_snapshot", "email", "phone", "payload",
               "client_secret", "clientSecret", "card", "payment_method"}
    return {
        k: v for k, v in detail.items()
        if k not in blocked and not isinstance(v, (bytes, bytearray))
    }
