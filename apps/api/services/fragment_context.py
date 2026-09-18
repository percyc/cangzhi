"""Read-time recovery of fragmented PDF pages, never a replacement for source.

Only already-authorized chunks may be passed here. Bounds, document/version
identity and the source anchor are checked again before returning context.
Normal prose and datasets retain their existing retrieval behavior.
"""
from __future__ import annotations

from collections import defaultdict
from sqlalchemy import JSON, column, func, select, true

from ..models.documents import Document, DocumentVersion

MAX_PAGE_BLOCKS = 512
MAX_CONTEXT_CHARS = 4000


def bounded_anchor_context(context: str, anchor: str, limit: int) -> str:
    """If budget requires truncation, retain the hit instead of the page header."""
    if len(context) <= limit:
        return context
    if limit <= 2:
        return "…"[:max(0, limit)]
    positions = [i for i, char in enumerate(context) if not char.isspace()]
    compact = "".join(context[i] for i in positions)
    needle = "".join(anchor.split())
    found = compact.find(needle) if needle else -1
    capacity = limit - 2
    start = 0
    if found >= 0:
        anchor_start = positions[found]
        anchor_end = positions[found + len(needle) - 1] + 1
        start = max(0, anchor_start - max(0, capacity - (anchor_end - anchor_start)) // 2)
    start = min(start, max(0, len(context) - capacity))
    end = start + capacity
    return ("…" if start else "") + context[start:end] + ("…" if end < len(context) else "")


def recover_page(blocks: list[dict], anchor: str, *, page: int) -> str | None:
    """Recover a complete small fragmented page, or decline without guessing.

    No normalization/rewrite of source text and no page or table crossing.
    Oversized pages are left to explicit bounded document exploration.
    """
    if not anchor or len(anchor) > 256 or not 8 <= len(blocks) <= MAX_PAGE_BLOCKS:
        return None
    if any(not isinstance(b, dict) or b.get("page") != page for b in blocks):
        return None
    if any(b.get("type") in {"table", "code_block"} for b in blocks):
        return None
    texts = [b.get("text") for b in blocks]
    if any(not isinstance(t, str) for t in texts):
        return None
    texts = [t for t in texts if t.strip()]
    if len(texts) < 8 or sum(len(t.strip()) <= 24 for t in texts) < len(texts) * .6:
        return None
    context = "\n".join(texts)
    if len(context) > MAX_CONTEXT_CHARS:
        return None
    # Whitespace differs between parser blocks and chunk joins. Check only;
    # the returned text always remains the original source sequence.
    compact_anchor = "".join(anchor.split())
    if not compact_anchor or compact_anchor not in "".join(context.split()):
        return None
    return context


async def recover_fragment_contexts(db, chunks) -> dict[int, str]:
    """Load at most one bounded source page per unique authorized anchor page."""
    groups = defaultdict(list)
    for chunk in chunks:
        if not chunk.is_current:
            continue
        extra = getattr(chunk, "extra", None) or {}
        if (chunk.page is not None and chunk.content and len(chunk.content) <= 256
                and not extra.get("sheet_name") and not extra.get("table_ranges")):
            groups[(chunk.document_id, chunk.document_version_id, chunk.page)].append(chunk)
    result = {}
    for (document_id, version_id, page), anchors in groups.items():
        # Expand JSON in the database instead of downloading a whole large PDF
        # or dataset payload. Source array order must not depend on query plans.
        if db.bind.dialect.name == "postgresql":
            blocks = func.jsonb_array_elements(
                DocumentVersion.structured_content["blocks"]
            ).table_valued(column("value", JSON), with_ordinality="ordinality").lateral()
            order = blocks.c.ordinality
        else:
            blocks = func.json_each(
                DocumentVersion.structured_content["blocks"]
            ).table_valued(column("value", JSON), "key")
            order = blocks.c.key
        statement = (
            select(blocks.c.value)
            .select_from(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .join(blocks, true())
            .where(
                DocumentVersion.id == version_id,
                DocumentVersion.document_id == document_id,
                Document.current_version_id == version_id,
                Document.is_deleted.is_(False),
                DocumentVersion.structured_content["document_type"].as_string() == "pdf",
                blocks.c.value["page"].as_integer() == page,
            )
            .order_by(order)
            .limit(MAX_PAGE_BLOCKS + 1)
        )
        page_blocks = list((await db.execute(statement)).scalars().all())
        for anchor in anchors:
            context = recover_page(page_blocks, anchor.content, page=page)
            if context is not None:
                result[anchor.id] = context
    return result
