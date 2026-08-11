"""One-off backfill for tool-generated PDF reports that predate owner tracking.

`GET /chat/file/{file_id}` authorizes a GENERATED_REPORT by comparing the
caller against `file_metadata["user_id"]`. Reports created before that field
was written have no owner recorded, so their download links 404. This script
recovers the owner from the chat transcript that contains the report link.

Safe to re-run: rows that already carry an owner are skipped.

Usage:
    python -m scripts.backfill_generated_report_owners
"""

from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.engine.sql_engine import SqlEngine
from onyx.db.file_record import backfill_generated_report_owners
from onyx.utils.logger import setup_logger

logger = setup_logger()


def main() -> None:
    SqlEngine.init_engine(pool_size=1, max_overflow=0)
    with get_session_with_current_tenant() as db_session:
        updated, unresolved = backfill_generated_report_owners(db_session)

    logger.info(
        f"Backfilled owner metadata for {updated} generated report(s); "
        f"{unresolved} could not be matched to a chat session."
    )


if __name__ == "__main__":
    main()
