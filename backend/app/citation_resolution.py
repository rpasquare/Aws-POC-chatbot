"""Resolve a citation to a page image and highlighted regions.

Requirements 8.4, 8.5, 8.6, 8.9, 12.2. A citation marker in a message's
prose was already resolved once, at answer time, to a `message_citations`
row holding `chunk_id` and `document_id` (see `app/citations.py`). This
module resolves that row the rest of the way: chunk -> page image ->
highlight regions, for the page viewer opened when a user clicks a
citation chip.

`chunk_id` is nullable on `message_citations` (Requirement 8.9: a citation
pointing at a since-purged document still renders, flagged unavailable
rather than erroring). `resolve_citation` returns an explicit "unavailable"
result for that case instead of raising, matching the same
degrade-gracefully behaviour the message list already has.

Region boxes are stored in `chunk_regions` as PDF-point boxes (x0, y0, x1,
y1), scoped to one page. `to_percentage_offsets` converts a box plus the
page's own width/height into percentage offsets of that page -- the
format the frontend overlay needs, since the page image can be displayed
at any size and only percentages stay correct at any display width
(Requirement 8.5).
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import Connection

from app import s3
from app.authz import require_role


class MessageNotFoundError(Exception):
    """Raised when the message id does not exist. `app/main.py` maps this
    and `NotAMemberError` to 404, the same "respond as though it does not
    exist" pattern used by `app/conversations.py`.
    """


@dataclass(frozen=True)
class RegionPercent:
    """One highlight box, as percentage offsets of the page image's
    displayed width/height. 0 to 100 on both axes by construction --
    `to_percentage_offsets` clamps nothing because stored boxes are always
    within their page's own dimensions (produced from the same page
    geometry at parse time).
    """

    x0_pct: float
    y0_pct: float
    x1_pct: float
    y1_pct: float


@dataclass(frozen=True)
class ResolvedCitation:
    """A citation resolved to a viewable page. `image_url` is a short-lived
    presigned URL (Requirement 12.2) -- never a durable public address.
    """

    available: bool
    document_id: str | None = None
    filename: str | None = None
    page_no: int | None = None
    page_width: float | None = None
    page_height: float | None = None
    image_url: str | None = None
    regions: tuple[RegionPercent, ...] = ()


def to_percentage_offsets(
    *, x0: float, y0: float, x1: float, y1: float, page_width: float, page_height: float
) -> RegionPercent:
    """Convert one PDF-point box to percentage offsets of its page.

    Requirement 8.5: the position must remain correct at any display
    size, so the overlay is built in percentages of the page's own
    width/height rather than any fixed pixel size -- the frontend applies
    these percentages to whatever size it actually renders the page
    image at.
    """
    return RegionPercent(
        x0_pct=(x0 / page_width) * 100,
        y0_pct=(y0 / page_height) * 100,
        x1_pct=(x1 / page_width) * 100,
        y1_pct=(y1 / page_height) * 100,
    )


def resolve_citation(conn: Connection, *, user_id: str, message_id: str, marker: int) -> ResolvedCitation:
    """Resolve one citation marker on one message to its page and regions.

    Requirement 8.6: resolution is derived entirely from stored chunk and
    page data, keyed by ids that never change after creation -- repeated
    calls, including after unrelated documents are added or reindexed,
    return the same chunk, page and regions.

    Requirement 8.9: a null `chunk_id` (the cited document was purged)
    yields `available=False` rather than a 404 or error -- the caller
    renders this as a disabled citation, not a broken one.

    Requirement 3.3/11.1 (resolved): any role on the message's project --
    admin, editor or viewer -- may open a cited page image, so this
    checks membership only, not a specific role. Raises
    `MessageNotFoundError` if the message itself does not exist, or
    `NotAMemberError` (from `app.authz`) if the caller is not a member of
    its project; `app/main.py` maps both to 404.
    """
    message_row = conn.execute(
        "select cs.project_id "
        "from chat_messages cm "
        "join chat_sessions cs on cs.id = cm.session_id "
        "where cm.id = %s",
        (message_id,),
    ).fetchone()
    if message_row is None:
        raise MessageNotFoundError(f"message {message_id!r} does not exist")

    require_role(conn, user_id, str(message_row["project_id"]), "admin", "editor", "viewer")

    row = conn.execute(
        "select chunk_id, document_id from message_citations where message_id = %s and marker = %s",
        (message_id, marker),
    ).fetchone()
    if row is None or row["chunk_id"] is None:
        return ResolvedCitation(available=False)

    chunk = conn.execute(
        "select c.id, c.document_id, c.page_from, d.filename "
        "from chunks c join documents d on d.id = c.document_id "
        "where c.id = %s",
        (row["chunk_id"],),
    ).fetchone()
    if chunk is None:
        # Requirement 8.9: the chunk row itself is gone (e.g. a hard purge
        # after the retention window), same degrade-gracefully treatment.
        return ResolvedCitation(available=False)

    page = conn.execute(
        "select width, height, image_key from document_pages where document_id = %s and page_no = %s",
        (chunk["document_id"], chunk["page_from"]),
    ).fetchone()
    if page is None:
        return ResolvedCitation(available=False)

    region_rows = conn.execute(
        "select x0, y0, x1, y1 from chunk_regions where chunk_id = %s and page_no = %s order by id",
        (chunk["id"], chunk["page_from"]),
    ).fetchall()
    regions = tuple(
        to_percentage_offsets(
            x0=float(r["x0"]),
            y0=float(r["y0"]),
            x1=float(r["x1"]),
            y1=float(r["y1"]),
            page_width=float(page["width"]),
            page_height=float(page["height"]),
        )
        for r in region_rows
    )

    return ResolvedCitation(
        available=True,
        document_id=str(chunk["document_id"]),
        filename=chunk["filename"],
        page_no=chunk["page_from"],
        page_width=float(page["width"]),
        page_height=float(page["height"]),
        image_url=s3.presigned_get(page["image_key"]),
        regions=regions,
    )


__all__ = ["MessageNotFoundError", "RegionPercent", "ResolvedCitation", "resolve_citation", "to_percentage_offsets"]
