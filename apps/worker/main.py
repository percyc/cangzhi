import sys
import os
import time
import signal
import structlog
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Add project root to path for absolute imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from apps.worker.core.config import settings
from apps.api.core.db import Base  # noqa: F401
from apps.worker.services.processor import process_pending_jobs

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ]
)

logger = structlog.get_logger()


running = True

def handle_shutdown(signum, frame):
    global running
    logger.info("Received shutdown signal, stopping gracefully")
    running = False

def main():
    global running
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    engine = create_engine(settings.database_url)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    logger.info("Worker starting", poll_interval=settings.poll_interval)

    while running:
        try:
            # Check database connectivity
            with SessionLocal() as session:
                session.execute(text("SELECT 1"))
                processed = process_pending_jobs(session)
                if processed > 0:
                    logger.info("Processed pending jobs", count=processed)
        except Exception as e:
            logger.error("Error processing jobs", error=str(e))

        if not running:
            break

        time.sleep(settings.poll_interval)

    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
