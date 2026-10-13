"""Chunk construction from parsed blocks.

Pure function, no DB or S3 access, so it can be unit tested in isolation and
called from the eventual index handler (task 4.4) just by passing it the
`blocks` list already read out of `parsed.json`.

Token counting: this codebase has no tokenizer dependency (checked
`pyproject.toml`; nothing like `tiktoken` is installed), and an exact token
count is not safety critical here, only the targets it is compared against.
`_approx_token_count` uses `len(text) // 4`, a common rough English-text
heuristic (~4 characters per token). This is an approximation, not an exact
count — if a real tokenizer is ever added for the embedding model, this
should be swapped to match it so chunk sizing tracks what Bedrock actually
sees.

Chunking algorithm (Requirement 6.6, 8.2):

1. Walk blocks in document order (already page/position ordered by the
   parse stage).
2. Accumulate blocks into the current chunk until adding the next block
   would push the running token count over `target_tokens`. A block is
   never split: if a single block's own token count already exceeds
   `target_tokens`, it becomes its own (oversized) chunk rather than being
   truncated.
3. A change in `heading_path` is a hard boundary: it always starts a new
   chunk, even if the current chunk is under target.
4. Overlap: when a new chunk starts because the *target was reached*, the
   trailing blocks of the chunk just closed are carried over as leading
   context for the new chunk, up to `overlap_tokens` worth (greedy from the
   end, stopping before exceeding the budget, never splitting a block to
   fit). Overlap does NOT apply when the new chunk starts because of a
   heading change — a new heading is a hard content boundary, not something
   to blend across. This also means overlapping blocks are *not* additional
   contributing regions for citation purposes beyond the chunk they were
   duplicated into; each physical block still ends up as a `chunk_regions`
   row on every chunk whose text actually contains it, carried or original.
5. The chunk's heading path is prepended to its text (`f"{heading_path}\n\n{body}"`)
   before token counting, so the stored `token_count` reflects what is
   actually embedded.
6. Every block whose text contributes to a chunk (including carried-over
   overlap blocks) produces one region entry: `(page_no, bbox)`. A block
   contributing to two chunks (the tail of one and the overlap head of the
   next) produces one region row in each.
7. `page_from`/`page_to` are the min/max `page_no` across a chunk's
   contributing blocks. `ordinal` is 1-based, matching the convention
   already used for `document_pages.page_no`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _approx_token_count(text: str) -> int:
    """Rough token estimate: ~4 characters per token for English prose.

    This is an approximation (see module docstring), not an exact tokenizer
    count. Empty text counts as zero tokens.
    """
    return len(text) // 4


@dataclass(frozen=True)
class ChunkRegion:
    """One contributing block's location, destined for a `chunk_regions` row."""

    page_no: int
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1


@dataclass(frozen=True)
class ChunkDraft:
    """Everything needed to insert a `chunks` row and its regions, minus the
    embedding (filled in by task 4.3's embedding client) and `embed_model`.
    """

    ordinal: int
    text: str
    heading_path: str | None
    page_from: int
    page_to: int
    token_count: int
    regions: tuple[ChunkRegion, ...] = field(default_factory=tuple)


@dataclass
class _Accumulator:
    """Mutable working state for the chunk currently being built."""

    heading_path: str | None
    blocks: list[dict]

    def body_text(self) -> str:
        return "\n\n".join(block["text"] for block in self.blocks)

    def token_count(self) -> int:
        return _approx_token_count(self._full_text())

    def _full_text(self) -> str:
        body = self.body_text()
        if self.heading_path:
            return f"{self.heading_path}\n\n{body}"
        return body

    def is_empty(self) -> bool:
        return not self.blocks


def _finalize(acc: _Accumulator, ordinal: int) -> ChunkDraft:
    full_text = acc._full_text()
    pages = [block["page_no"] for block in acc.blocks]
    regions = tuple(
        ChunkRegion(page_no=block["page_no"], bbox=tuple(block["bbox"])) for block in acc.blocks
    )
    return ChunkDraft(
        ordinal=ordinal,
        text=full_text,
        heading_path=acc.heading_path,
        page_from=min(pages),
        page_to=max(pages),
        token_count=_approx_token_count(full_text),
        regions=regions,
    )


def _overlap_blocks(blocks: list[dict], overlap_tokens: int) -> list[dict]:
    """Trailing blocks from a closed chunk that fit within the overlap budget.

    Greedy from the end: keep adding whole blocks moving backwards while the
    running token total stays within `overlap_tokens`. A block is never
    split to fit; if even the single last block exceeds the budget, no
    overlap is carried (an oversized block is already its own chunk by the
    "never split a block" rule, so this is the expected outcome there).
    """
    if overlap_tokens <= 0:
        return []
    carried: list[dict] = []
    running_tokens = 0
    for block in reversed(blocks):
        block_tokens = _approx_token_count(block["text"])
        if carried and running_tokens + block_tokens > overlap_tokens:
            break
        carried.insert(0, block)
        running_tokens += block_tokens
        if running_tokens >= overlap_tokens:
            break
    return carried


def build_chunks(
    blocks: list[dict],
    *,
    target_tokens: int,
    overlap_tokens: int,
) -> list[ChunkDraft]:
    """Group parsed blocks into chunk drafts.

    `blocks` must already be in document order (page then position), as
    produced by the parse stage. Returns one `ChunkDraft` per chunk, in
    document order, 1-based `ordinal`. Pure function: no DB or S3 access.
    """
    if not blocks:
        return []

    drafts: list[ChunkDraft] = []
    acc = _Accumulator(heading_path=blocks[0].get("heading_path"), blocks=[])
    ordinal = 1
    pending_overlap: list[dict] = []

    for block in blocks:
        heading_path = block.get("heading_path")
        block_tokens = _approx_token_count(block["text"])
        is_oversized_block = block_tokens > target_tokens

        heading_changed = not acc.is_empty() and heading_path != acc.heading_path
        would_exceed_target = (
            not acc.is_empty()
            and not heading_changed
            and acc.token_count() + block_tokens > target_tokens
        )

        if heading_changed or would_exceed_target:
            drafts.append(_finalize(acc, ordinal))
            ordinal += 1
            # Overlap only carries forward across a target-exceeded boundary,
            # never across a heading change (a new heading is a hard
            # boundary — see module docstring), and never into a chunk
            # about to receive an oversized block: an oversized block must
            # end up alone in its own chunk, not sharing one with carried
            # context, or it would appear duplicated across two chunks.
            pending_overlap = [] if (heading_changed or is_oversized_block) else _overlap_blocks(acc.blocks, overlap_tokens)
            acc = _Accumulator(heading_path=heading_path, blocks=list(pending_overlap))

        acc.blocks.append(block)

        if is_oversized_block:
            # The block just added already exceeds target on its own —
            # isolate it immediately as its own chunk rather than letting
            # a following block be appended to it first (which would
            # silently grow an already-oversized chunk further).
            drafts.append(_finalize(acc, ordinal))
            ordinal += 1
            acc = _Accumulator(heading_path=heading_path, blocks=[])

    if not acc.is_empty():
        drafts.append(_finalize(acc, ordinal))

    return drafts


__all__ = ["ChunkDraft", "ChunkRegion", "build_chunks"]
