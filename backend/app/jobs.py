"""Ingestion job queue: claiming, retry/backoff, and the abandoned-job reaper.

The queue is a Postgres table rather than a message broker (see design
"Why the queue is a database table"). Concurrency safety comes from
`SELECT ... FOR UPDATE SKIP LOCKED`: a worker locks the row it claims, other
workers step over locked rows rather than blocking. Requirement 6.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg import Connection

from app.config import get_settings


@dataclass(frozen=True)
class Job:
    id: int
    document_id: str
    stage: str
    attempts: int
    max_attempts: int


def claim_job(conn: Connection, *, worker_id: str) -> Job | None:
    """Claim exactly one queued, due job, or None if there is no work.

    `FOR UPDATE SKIP LOCKED` means a concurrent caller never blocks on this
    row and never claims it twice: it simply sees the next queued row, or
    none. The caller is expected to commit promptly so the `running` status
    is visible to the reaper if the process dies before finishing.
    """
    row = conn.execute(
        "select id, document_id, stage, attempts, max_attempts "
        "from ingestion_jobs "
        "where status = 'queued' and run_after <= now() "
        "order by run_after "
        "for update skip locked "
        "limit 1"
    ).fetchone()
    if row is None:
        return None

    conn.execute(
        "update ingestion_jobs "
        "set status = 'running', claimed_at = now(), claimed_by = %s, updated_at = now() "
        "where id = %s",
        (worker_id, row["id"]),
    )
    return Job(
        id=row["id"],
        document_id=str(row["document_id"]),
        stage=row["stage"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
    )


def backoff_seconds(attempts: int) -> float:
    """Exponential backoff: 30s * 2^attempts, per the design's failure handling."""
    return 30.0 * (2**attempts)


def next_attempt_outcome(attempts: int, max_attempts: int) -> tuple[int, str]:
    """Pure decision: the new attempt count and the resulting terminal or
    retry status. Requirement 6.9: retries are bounded and the job always
    reaches a terminal state. Extracted from `mark_failed_or_retry` so the
    bookkeeping can be property-tested without a database.
    """
    attempts = attempts + 1
    status = "failed" if attempts >= max_attempts else "queued"
    return attempts, status


def mark_done(conn: Connection, job_id: int) -> None:
    conn.execute(
        "update ingestion_jobs set status = 'done', updated_at = now() where id = %s",
        (job_id,),
    )


def mark_failed_or_retry(conn: Connection, job: Job, *, error: str) -> str:
    """Increment attempts; requeue with backoff below the limit, else fail.

    Requirement 6.9: retries are bounded. Returns the resulting job status,
    either 'queued' or 'failed'.
    """
    attempts, status = next_attempt_outcome(job.attempts, job.max_attempts)
    if status == "failed":
        conn.execute(
            "update ingestion_jobs "
            "set status = 'failed', attempts = %s, last_error = %s, updated_at = now() "
            "where id = %s",
            (attempts, error, job.id),
        )
        return "failed"

    delay = backoff_seconds(attempts)
    conn.execute(
        "update ingestion_jobs "
        "set status = 'queued', attempts = %s, last_error = %s, "
        "    claimed_at = null, claimed_by = null, "
        "    run_after = now() + make_interval(secs => %s), updated_at = now() "
        "where id = %s",
        (attempts, error, delay, job.id),
    )
    return "queued"


def reap_abandoned_jobs(conn: Connection) -> int:
    """Return `running` jobs older than the stale threshold to `queued`.

    Requirement 6.11: a worker that dies mid-job does not lose the work.
    """
    settings = get_settings()
    result = conn.execute(
        "update ingestion_jobs "
        "set status = 'queued', claimed_at = null, claimed_by = null, updated_at = now() "
        "where status = 'running' "
        "  and claimed_at < now() - make_interval(secs => %s)",
        (settings.job_stale_after_seconds,),
    )
    return result.rowcount


__all__ = [
    "Job",
    "claim_job",
    "backoff_seconds",
    "next_attempt_outcome",
    "mark_done",
    "mark_failed_or_retry",
    "reap_abandoned_jobs",
]
