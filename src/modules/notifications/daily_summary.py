"""The daily saves-and-follows push (BACKEND_CHANGES B7, `NotifSettings` "Daily summary: One
notification a day with everything grouped").

With the default `savesAndFollowsMode: "daily"`, a save or follow writes its Activity row
(bundled, see `notifications_dao._add_to_bundle`) but sends no push; the row is marked
`summaryPending`. Once a day this sends each person one push for everything still unread, and
clears the marks.

Safe to run twice: the marks are cleared and committed before any push goes out, so a
redelivered run finds nothing left to send (at worst a crash between the two loses a summary;
it never sends one twice).
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from src.modules.notifications.notifications_dao import BUNDLED_TYPES
from src.shared.config.database import SessionLocal
from src.shared.models.notification import Notification
from src.shared.models.user import User
from src.shared.notification.push_service import send_push
from src.shared.utils.logger import get_logger

logger = get_logger(__name__)

SUMMARY_PUSH_TYPE = "saves_and_follows_summary"
SUMMARY_TITLE = "Today on Studio 3"
# Rows touched within this window can still be pending (the job runs daily; two days covers a
# skipped run).
_LOOKBACK = timedelta(days=2)


def summary_text(saves: int, first_saver: Optional[str], follows: int, first_follower: Optional[str]) -> Optional[str]:
    """One sentence for the day: "Amara Osei saved your work and 3 new people followed you."."""
    parts = []
    if saves == 1:
        parts.append(f"{first_saver or 'Someone'} saved your work")
    elif saves > 1:
        parts.append(f"{saves} people saved your work")
    if follows == 1:
        parts.append(f"{first_follower or 'Someone'} followed you")
    elif follows > 1:
        parts.append(f"{follows} new people followed you")
    if not parts:
        return None
    return " and ".join(parts) + "."


def _first_name(row: Notification) -> Optional[str]:
    actors = (row.payload or {}).get("actors") or []
    return actors[0].get("name") if actors else None


def send_daily_summaries(db: Optional[Session] = None, now: Optional[datetime] = None) -> int:
    """Sends the day's summary to everyone with pending saves or follows. Returns how many
    pushes went out."""
    own = db is None
    db = db or SessionLocal()
    try:
        now = now or datetime.now(timezone.utc)
        rows = (
            db.query(Notification)
            .filter(Notification.type.in_(BUNDLED_TYPES), Notification.created_at >= now - _LOOKBACK)
            .order_by(Notification.created_at.desc())
            .all()
        )
        pending: dict = defaultdict(list)
        for row in rows:
            if (row.payload or {}).get("summaryPending"):
                pending[row.user_id].append(row)
        if not pending:
            return 0

        # Clear first: a second run must find nothing to send.
        for items in pending.values():
            for row in items:
                row.payload = {k: v for k, v in (row.payload or {}).items() if k != "summaryPending"}
        db.commit()

        sent = 0
        for user_id, items in pending.items():
            user = db.get(User, user_id)
            if user is None:
                continue
            push_prefs = (user.notification_preferences or {}).get("push", {})
            # Seen in the app already, or that kind turned off: not in the summary.
            unread = [r for r in items if not r.read and push_prefs.get(r.type, True)]
            saves = [r for r in unread if r.type == "save"]
            follows = [r for r in unread if r.type == "follow"]
            body = summary_text(
                sum(int((r.payload or {}).get("count") or 1) for r in saves),
                _first_name(saves[0]) if saves else None,
                sum(int((r.payload or {}).get("count") or 1) for r in follows),
                _first_name(follows[0]) if follows else None,
            )
            if body is None:
                continue
            send_push(user_id, SUMMARY_TITLE, body, data={"type": SUMMARY_PUSH_TYPE})
            sent += 1
        logger.info("Daily saves-and-follows summary sent to %d people", sent)
        return sent
    finally:
        if own:
            db.close()
