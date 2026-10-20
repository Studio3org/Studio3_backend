"""Reports DAO."""
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.shared.models.report import Report, REPORT_OPEN


def find_open_report(
    db: Session, reporter_id: uuid.UUID, target_type: str, target_id: uuid.UUID
) -> Optional[Report]:
    return db.execute(
        select(Report).where(
            Report.reporter_id == reporter_id,
            Report.target_type == target_type,
            Report.target_id == target_id,
            Report.status == REPORT_OPEN,
        )
    ).scalar_one_or_none()


def create_report(
    db: Session,
    reporter_id: uuid.UUID,
    target_type: str,
    target_id: uuid.UUID,
    reason: str,
    details: Optional[str],
) -> Report:
    report = Report(
        id=uuid.uuid4(),
        reporter_id=reporter_id,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
        details=details,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


def list_my_reports(db: Session, reporter_id: uuid.UUID) -> list[Report]:
    return list(
        db.execute(
            select(Report)
            .where(Report.reporter_id == reporter_id)
            .order_by(Report.created_at.desc())
        )
        .scalars()
        .all()
    )


def list_reports(db: Session, status: Optional[str] = "open") -> list[Report]:
    q = select(Report)
    if status:
        q = q.where(Report.status == status)
    return list(db.execute(q.order_by(Report.created_at.asc())).scalars().all())


def count_open_reports(db: Session) -> int:
    from sqlalchemy import func

    return db.execute(
        select(func.count(Report.id)).where(Report.status == REPORT_OPEN)
    ).scalar_one()


def get_report(db: Session, report_id: uuid.UUID) -> Optional[Report]:
    return db.get(Report, report_id)


def resolve_report(
    db: Session, report: Report, admin_id: uuid.UUID, status: str, note: str
) -> Report:
    report.status = status
    report.resolved_by_admin_id = admin_id
    report.resolution_note = note
    report.resolved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(report)
    return report


def remove_reported_target(db: Session, report: Report) -> bool:
    """Take down the content a report points at. Returns False if there's nothing to remove.

    Pieces and scenes are soft-deleted (deleted_at), the same as an owner deleting them, so
    order history that references a piece survives. Comments have no soft-delete and nothing
    references them, so they're removed outright. Accounts aren't removable from here.
    """
    from src.shared.models.piece import Piece
    from src.shared.models.post import Post
    from src.shared.models.social import Comment

    if report.target_type in ("piece", "post"):
        model = Piece if report.target_type == "piece" else Post
        row = db.get(model, report.target_id)
        if not row or row.deleted_at is not None:
            return False
        row.deleted_at = datetime.now(timezone.utc)
        db.commit()
        return True
    if report.target_type == "comment":
        row = db.get(Comment, report.target_id)
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True
    return False
