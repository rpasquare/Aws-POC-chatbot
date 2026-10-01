"""Worker entry point.

Runs everything slow: reading documents, producing page images, preparing
searchable content. Never serves an HTTP request.

Claims one job at a time and dispatches by stage to a handler map. Stage
handlers (parse, index) are registered as they are built; an unregistered
stage is a startup-time configuration error, not a per-job failure.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
import uuid
from collections.abc import Callable
from types import FrameType

from psycopg import Connection

from app import db, jobs
from app.config import get_settings
from app.pipeline import run_parse_job

log = logging.getLogger("app.worker")

_stop = False

StageHandler = Callable[[Connection, jobs.Job], None]

HANDLERS: dict[str, StageHandler] = {"parse": run_parse_job}
"""The index handler (task 4.4) registers itself here as 'index' when it lands."""


def _request_stop(signum: int, _frame: FrameType | None) -> None:
    global _stop
    _stop = True
    log.info("shutdown requested signal=%s", signum)


def worker_id() -> str:
    """Identifier recorded against claimed jobs."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def process_one(identity: str) -> bool:
    """Claim and run at most one job. Returns True if a job was claimed.

    Claiming is committed in its own transaction, so the row is durably
    `running` and visible to the reaper the instant it's claimed, regardless
    of what happens next. The handler's own outcome (success, or failure
    with retry/backoff) is committed in a second, separate transaction, so a
    process killed mid-handler leaves the job `running` for the reaper to
    reclaim rather than silently losing the claim. Requirement 6.11.
    """
    with db.connection() as conn:
        job = jobs.claim_job(conn, worker_id=identity)
    if job is None:
        return False

    handler = HANDLERS.get(job.stage)
    if handler is None:
        with db.connection() as conn:
            jobs.mark_failed_or_retry(conn, job, error=f"no handler registered for stage {job.stage!r}")
        return True

    try:
        with db.connection() as conn:
            handler(conn, job)
    except Exception as exc:  # noqa: BLE001 - any handler failure is a retryable job failure
        log.exception("job failed id=%s stage=%s", job.id, job.stage)
        with db.connection() as conn:
            jobs.mark_failed_or_retry(conn, job, error=str(exc))
    else:
        with db.connection() as conn:
            jobs.mark_done(conn, job.id)
    return True


def run() -> None:
    settings = get_settings()  # fails fast when configuration is incomplete
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    db.open_pool()
    identity = worker_id()
    log.info("worker started id=%s region=%s", identity, settings.aws_region)
    last_reap = 0.0
    try:
        while not _stop:
            now = time.monotonic()
            if now - last_reap >= settings.job_stale_after_seconds:
                with db.connection() as conn:
                    reaped = jobs.reap_abandoned_jobs(conn)
                if reaped:
                    log.info("reaped abandoned jobs count=%d", reaped)
                last_reap = now
            claimed = process_one(identity)
            if not claimed:
                time.sleep(settings.job_poll_seconds)
    finally:
        db.close_pool()
        log.info("worker stopped id=%s", identity)


if __name__ == "__main__":
    run()
