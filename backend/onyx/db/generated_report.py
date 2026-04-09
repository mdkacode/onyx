"""Database operations for the generated_report table."""

from uuid import UUID

from sqlalchemy.orm import Session

from onyx.db.models import GeneratedReport


def create_generated_report(
    db_session: Session,
    user_id: UUID,
    title: str,
    s3_object_key: str,
) -> GeneratedReport:
    """Insert a new generated report record and return it."""
    report = GeneratedReport(
        user_id=user_id,
        title=title,
        s3_object_key=s3_object_key,
    )
    db_session.add(report)
    db_session.commit()
    return report
