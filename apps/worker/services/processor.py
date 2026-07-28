import datetime
from sqlalchemy.orm import Session
from sqlalchemy import text

import structlog

logger = structlog.get_logger()


def process_pending_jobs(session: Session) -> int:
    """Find and count pending processing jobs - skeleton implementation

    Actual processing will be added in M1.
    """
    now = datetime.datetime.now(datetime.timezone.utc)

    # Find available jobs: created or failed with retry available
    query = text("""
        SELECT id FROM processing_jobs
        WHERE status IN ('created', 'retry')
          AND (next_retry_at IS NULL OR next_retry_at <= :now)
        ORDER BY created_at ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 10
    """)

    result = session.execute(query, {"now": now})
    job_ids = [row[0] for row in result]

    if len(job_ids) > 0:
        logger.info("Found pending jobs", count=len(job_ids),
                    note="Processing will be implemented in M1")

    session.commit()
    return len(job_ids)
