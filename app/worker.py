import argparse
import logging
import signal
import threading
import time

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.auth.service import cleanup_auth
from app.core.config import get_settings
from app.core.db import make_engine
from app.core.models import ServiceHeartbeat
from app.core.readiness import HEARTBEAT_INTERVAL, WORKER_NAME, database_checks
from app.videos.processing import cleanup_media, process_one

logger = logging.getLogger("foxtube.worker")


def main() -> int:
    parser = argparse.ArgumentParser(description="FoxTube background worker")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    engine = make_engine(settings, pool_size=1)
    if args.healthcheck:
        try:
            return 0 if all(v == "ok" for v in database_checks(engine).values()) else 1
        finally:
            engine.dispose()

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    logger.info("Worker started; video jobs and cleanup enabled")
    next_cleanup = 0.0
    try:
        while not stop.is_set():
            try:
                statement = insert(ServiceHeartbeat).values(
                    name=WORKER_NAME, last_seen=func.clock_timestamp()
                )
                cleanup_due = time.monotonic() >= next_cleanup
                with engine.begin() as connection:
                    connection.execute(
                        statement.on_conflict_do_update(
                            index_elements=[ServiceHeartbeat.name],
                            set_={"last_seen": func.clock_timestamp()},
                        )
                    )
                    if cleanup_due:
                        cleanup_auth(connection)
                if cleanup_due:
                    cleanup_media(engine, settings.media_root)
                    next_cleanup = time.monotonic() + 60
                if process_one(engine, settings.media_root, stop):
                    continue
            except SQLAlchemyError:
                logger.warning("Heartbeat failed; retrying after the polling interval")
            except OSError:
                logger.warning("Media storage unavailable; retrying later")
            stop.wait(min(HEARTBEAT_INTERVAL, 2))
    finally:
        engine.dispose()
        logger.info("Worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
