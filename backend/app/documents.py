"""Document status state machine.

Status only ever moves along a declared edge. Requirement 6.2: the system
exposes a status that distinguishes pending, parsing, indexing, ready and
failed, and nothing else is reachable.

    awaiting_upload -> pending_parse
    pending_parse   -> parsing
    parsing         -> indexing
    parsing         -> failed
    indexing        -> ready
    indexing        -> failed
    failed          -> pending_parse   (retry a failed parse)
    failed          -> indexing        (retry a failed index)
    ready           -> indexing        (reindex / reprocessing, Requirement 11)
    ready           -> deleted         (soft delete, Requirement 10.1)
    deleted         -> ready           (restore, Requirement 10.4)
    deleted         -> purged          (retention elapsed, Requirement 10.5)
"""

from __future__ import annotations

DocumentStatus = str

STATUSES: frozenset[DocumentStatus] = frozenset(
    {
        "awaiting_upload",
        "pending_parse",
        "parsing",
        "indexing",
        "ready",
        "failed",
        "deleted",
        "purged",
    }
)

TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    "awaiting_upload": frozenset({"pending_parse"}),
    "pending_parse": frozenset({"parsing"}),
    "parsing": frozenset({"indexing", "failed"}),
    "indexing": frozenset({"ready", "failed"}),
    "failed": frozenset({"pending_parse", "indexing"}),
    "ready": frozenset({"indexing", "deleted"}),
    "deleted": frozenset({"ready", "purged"}),
    "purged": frozenset(),
}


class IllegalTransitionError(ValueError):
    """Raised when a transition is not present in the declared table."""

    def __init__(self, current: DocumentStatus, target: DocumentStatus) -> None:
        self.current = current
        self.target = target
        super().__init__(f"cannot transition document status from {current!r} to {target!r}")


def is_legal_transition(current: DocumentStatus, target: DocumentStatus) -> bool:
    """True when `target` is a declared successor of `current`."""
    return target in TRANSITIONS.get(current, frozenset())


def validate_transition(current: DocumentStatus, target: DocumentStatus) -> DocumentStatus:
    """Return `target` if the transition is declared, else raise.

    Unknown statuses are rejected the same way as undeclared edges, so a typo
    in either argument fails loudly rather than silently no-op-ing.
    """
    if current not in STATUSES:
        raise IllegalTransitionError(current, target)
    if target not in STATUSES:
        raise IllegalTransitionError(current, target)
    if not is_legal_transition(current, target):
        raise IllegalTransitionError(current, target)
    return target


def apply_transition(
    conn,
    document_id: str,
    target: DocumentStatus,
    *,
    failure_reason: str | None = None,
) -> DocumentStatus:
    """Validate and apply a status transition inside the caller's transaction.

    Reads the current status with `FOR UPDATE` so concurrent transitions on
    the same document serialise rather than racing, raises
    `IllegalTransitionError` for anything not in the declared table, and
    otherwise writes the new status in the same statement.
    """
    row = conn.execute(
        "select status from documents where id = %s for update",
        (document_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"no document with id {document_id!r}")

    current = row["status"]
    validate_transition(current, target)

    ready_at_clause = ", ready_at = now()" if target == "ready" else ""
    conn.execute(
        f"update documents set status = %s, failure_reason = %s{ready_at_clause} where id = %s",
        (target, failure_reason, document_id),
    )
    return target
