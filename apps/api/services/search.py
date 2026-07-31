"""Document search over the chunk layer.

The search is intentionally narrow for the M3 first cut:

* Full-text search over chunk text using PostgreSQL ``tsvector`` /
  ``to_tsquery`` in production. The test environment runs on SQLite,
  where we transparently fall back to a case-insensitive ``LIKE``
  search. The API surface is identical, so the front-end never has to
  branch on the database backend.
* Filters by category (slug or id), tag (slug or id) and source type.
* Results are grouped by document and the best-scoring child chunk is
  surfaced along with the heading path, page, paragraph index and
  source span. A short highlight snippet is included so the UI can
  render the hit without a second round-trip.

Embedding, vector recall, RRF fusion and Reranker are explicitly
out of scope here (see ``docs/ROADMAP.md`` M3 later items).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Integer, and_, case, false, func, or_, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from ..models.chunks import DocumentChunk
from ..models.documents import Document, DocumentSourceType, DocumentVersion
from ..models.taxonomy import (
    Category,
    DocumentCategory,
    DocumentTag,
    Tag,
)

MAX_LIMIT = 50
DEFAULT_LIMIT = 20
SNIPPET_RADIUS = 80
SNIPPET_MAX_LENGTH = 280
_TABLE_LOCATION_KEYS = {
    "sheet_name",
    "region_index",
    "row_start",
    "row_end",
    "header_row",
    "column_names",
    "row_fragmented",
    "table_ranges",
}


def table_location_from_extra(value: dict | None) -> dict:
    extra = value or {}
    if not (extra.get("sheet_name") or extra.get("table_ranges")):
        return {}
    return {key: extra[key] for key in _TABLE_LOCATION_KEYS if key in extra}


@dataclass
class SearchHit:
    """A single document hit with the best matching child chunk."""

    document_id: int
    document_version_id: int
    title: str
    source_type: str
    source_url: str | None
    updated_at: Any
    chunk_id: int
    parent_id: int | None
    chunk_type: str
    heading_path: list[str]
    page: int | None
    paragraph_index: int | None
    source_start: int | None
    source_end: int | None
    table_location: dict
    score: float
    snippet: str
    highlights: list[dict]
    categories: list[dict]
    tags: list[dict]

    def to_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "title": self.title,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "updated_at": (
                self.updated_at.isoformat()
                if hasattr(self.updated_at, "isoformat")
                else self.updated_at
            ),
            "score": self.score,
            "chunk": {
                "id": self.chunk_id,
                "parent_id": self.parent_id,
                "type": self.chunk_type,
                "heading_path": list(self.heading_path),
                "page": self.page,
                "paragraph_index": self.paragraph_index,
                "source_start": self.source_start,
                "source_end": self.source_end,
                "table_location": dict(self.table_location),
            },
            "snippet": self.snippet,
            "highlights": self.highlights,
            "categories": self.categories,
            "tags": self.tags,
        }


@dataclass
class SearchResult:
    query: str
    backend: str
    total: int
    hits: list[SearchHit]
    limit: int
    offset: int
    filters: dict
    retrieval: dict | None = None

    def to_dict(self) -> dict:
        payload = {
            "query": self.query,
            "backend": self.backend,
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
            "filters": self.filters,
            "hits": [hit.to_dict() for hit in self.hits],
        }
        if self.retrieval is not None:
            payload["retrieval"] = self.retrieval
        return payload


def normalize_query(query: str) -> list[str]:
    """Extract the search terms from the user query.

    The terms drive both the PostgreSQL ``to_tsquery`` payload and the
    SQLite ``LIKE`` patterns. We strip very short tokens (single CJK
    characters) but keep the full original string for the
    ``websearch_to_tsquery`` style ranking.

    Natural-language CJK expansion is intentionally handled by the Q&A
    retrieval layer. Keeping ordinary search terms compact avoids noisy
    matches and overlapping highlights on the search page.
    """

    cleaned = (query or "").strip()
    if not cleaned:
        return []
    raw_tokens = re.findall(r"[\w一-鿿]+", cleaned, flags=re.UNICODE)
    tokens: list[str] = []
    for token in raw_tokens:
        if not token:
            continue
        if len(token) == 1 and re.match(r"[\u4e00-\u9fff]", token):
            # Single Chinese characters produce too many false
            # positives, so we keep them as part of the phrase but
            # not as standalone query terms.
            continue
        if len(token) > 32:
            token = token[:32]
        tokens.append(token)
    if not tokens:
        return [cleaned] if cleaned else []
    # Deduplicate while preserving order to avoid sending the same
    # LIKE clause twice to PostgreSQL.
    seen: set[str] = set()
    deduped: list[str] = []
    for item in tokens:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


_CJK_RANGE = (0x3400, 0x9FFF)


def _is_cjk_char(ch: str) -> bool:
    if not ch:
        return False
    code = ord(ch)
    return _CJK_RANGE[0] <= code <= _CJK_RANGE[1]


def extract_cjk_ngrams(
    text: str,
    *,
    min_gram: int = 2,
    max_gram: int = 3,
    max_ngrams: int = 24,
) -> list[str]:
    """Return 2/3-grams extracted from a CJK run of ``text``.

    Non-CJK characters break the run; punctuation is ignored. The
    output is bounded by ``max_ngrams`` so an attacker cannot blow up
    the query with a huge question.
    """

    if not text:
        return []
    grams: list[str] = []
    seen: set[str] = set()
    run: list[str] = []
    for ch in text:
        if _is_cjk_char(ch):
            run.append(ch)
            continue
        grams.extend(
            _ngrams_from_run(run, min_gram=min_gram, max_gram=max_gram, seen=seen)
        )
        run = []
    grams.extend(_ngrams_from_run(run, min_gram=min_gram, max_gram=max_gram, seen=seen))
    if len(grams) > max_ngrams:
        grams = grams[:max_ngrams]
    return grams


def _ngrams_from_run(
    run: list[str],
    *,
    min_gram: int,
    max_gram: int,
    seen: set[str],
) -> list[str]:
    out: list[str] = []
    length = len(run)
    for size in range(min_gram, max_gram + 1):
        if length < size:
            break
        for start in range(length - size + 1):
            gram = "".join(run[start : start + size])
            if gram in seen:
                continue
            seen.add(gram)
            out.append(gram)
    return out


async def search_documents(
    db: AsyncSession,
    *,
    query: str,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    category_ids: Sequence[int] | None = None,
    category_slugs: Sequence[str] | None = None,
    tag_ids: Sequence[int] | None = None,
    tag_slugs: Sequence[str] | None = None,
    source_types: Sequence[str] | None = None,
    document_ids: Sequence[int] | None = None,
    connector_ids: Sequence[int] | None = None,
    matches_none: bool = False,
) -> SearchResult:
    """Run hybrid retrieval, transparently degrading to lexical search."""

    effective_document_ids = list(document_ids or [])
    if connector_ids and not matches_none:
        from .knowledge_scopes import resolve_connector_document_ids

        connector_document_ids = await resolve_connector_document_ids(
            db, connector_ids
        )
        if not connector_document_ids:
            matches_none = True
        elif effective_document_ids:
            effective_document_ids = sorted(
                set(effective_document_ids).intersection(connector_document_ids)
            )
            matches_none = not effective_document_ids
        else:
            effective_document_ids = connector_document_ids

    requested_limit = max(1, min(limit, MAX_LIMIT))
    requested_offset = max(0, offset)
    candidate_limit = min(
        MAX_LIMIT,
        max(requested_limit + requested_offset, requested_limit * 4),
    )
    lexical = await _search_documents_lexical(
        db,
        query=query,
        limit=candidate_limit,
        offset=0,
        category_ids=category_ids,
        category_slugs=category_slugs,
        tag_ids=tag_ids,
        tag_slugs=tag_slugs,
        source_types=source_types,
        document_ids=effective_document_ids,
        connector_ids=connector_ids,
        matches_none=matches_none,
    )
    if not (query or "").strip():
        return lexical

    from .hybrid_retrieval import recall_vector_chunks, rrf_scores

    vector = await recall_vector_chunks(
        db,
        query=query,
        filters=lexical.filters,
        limit=candidate_limit,
    )
    lexical.retrieval = vector.status.to_dict()
    if not vector.status.vector_used:
        lexical.limit = requested_limit
        lexical.offset = requested_offset
        lexical.hits = lexical.hits[
            requested_offset : requested_offset + requested_limit
        ]
        return lexical

    vector_hits = await _build_hits(db, vector.rows, normalize_query(query))
    lexical_ids = [hit.chunk_id for hit in lexical.hits]
    vector_ids = [hit.chunk_id for hit in vector_hits]
    scores = rrf_scores(lexical_ids, vector_ids)
    hit_by_chunk = {hit.chunk_id: hit for hit in [*lexical.hits, *vector_hits]}
    ordered = sorted(
        hit_by_chunk.values(),
        key=lambda hit: (-scores.get(hit.chunk_id, 0.0), hit.chunk_id),
    )
    # Search results are documents, not chunks. Keep the best fused
    # chunk for each document before applying pagination.
    best_documents: list[SearchHit] = []
    seen_documents: set[int] = set()
    for hit in ordered:
        if hit.document_id in seen_documents:
            continue
        seen_documents.add(hit.document_id)
        hit.score = scores.get(hit.chunk_id, 0.0)
        best_documents.append(hit)
    lexical.backend = "hybrid"
    lexical.total = max(lexical.total, len(best_documents))
    lexical.limit = requested_limit
    lexical.offset = requested_offset
    lexical.hits = best_documents[requested_offset : requested_offset + requested_limit]
    return lexical


async def _search_documents_lexical(
    db: AsyncSession,
    *,
    query: str,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    category_ids: Sequence[int] | None = None,
    category_slugs: Sequence[str] | None = None,
    tag_ids: Sequence[int] | None = None,
    tag_slugs: Sequence[str] | None = None,
    source_types: Sequence[str] | None = None,
    document_ids: Sequence[int] | None = None,
    connector_ids: Sequence[int] | None = None,
    matches_none: bool = False,
) -> SearchResult:
    """Run a search and return a :class:`SearchResult`.

    The implementation picks the search backend based on the bound
    dialect. PostgreSQL uses ``to_tsvector`` / ``to_tsquery`` with a
    GIN index (set up in the migration); SQLite degrades to a
    case-insensitive ``LIKE`` over each normalized term. Any failure
    with the PostgreSQL path (for example missing extensions during
    local dev) transparently falls back to the LIKE backend so the
    API stays usable.
    """

    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    cleaned = (query or "").strip()
    terms = normalize_query(cleaned)
    filters = {
        "category_ids": list(category_ids or []),
        "category_slugs": list(category_slugs or []),
        "tag_ids": list(tag_ids or []),
        "tag_slugs": list(tag_slugs or []),
        "source_types": list(source_types or []),
        "document_ids": list(document_ids or []),
        "connector_ids": list(connector_ids or []),
        "matches_none": matches_none,
    }

    if not cleaned:
        if any(filters.values()):
            return await _browse_filtered(
                db,
                limit=limit,
                offset=offset,
                filters=filters,
            )
        return SearchResult(
            query="",
            backend=_detect_backend(db),
            total=0,
            hits=[],
            limit=limit,
            offset=offset,
            filters=filters,
        )

    backend = _detect_backend(db)
    try:
        if backend == "postgresql":
            return await _search_postgres(
                db,
                query=cleaned,
                terms=terms,
                limit=limit,
                offset=offset,
                filters=filters,
            )
    except ProgrammingError:
        # Extension missing or unsupported configuration: retry
        # through the LIKE backend.
        pass
    return await _search_like(
        db,
        query=cleaned,
        terms=terms,
        limit=limit,
        offset=offset,
        filters=filters,
    )


async def _browse_filtered(
    db: AsyncSession,
    *,
    limit: int,
    offset: int,
    filters: dict,
) -> SearchResult:
    """Browse filtered documents without invoking lexical/vector search."""

    stmt = (
        select(
            DocumentChunk.id.label("chunk_id"),
            DocumentChunk.parent_id.label("parent_id"),
            DocumentChunk.document_id.label("document_id"),
            DocumentChunk.document_version_id.label("document_version_id"),
            DocumentChunk.chunk_type.label("chunk_type"),
            DocumentChunk.heading_path.label("heading_path"),
            DocumentChunk.page.label("page"),
            DocumentChunk.paragraph_index.label("paragraph_index"),
            DocumentChunk.source_start.label("source_start"),
            DocumentChunk.source_end.label("source_end"),
            DocumentChunk.content.label("content"),
            DocumentChunk.extra.label("chunk_extra"),
            Document.title.label("title"),
            Document.source_type.label("source_type"),
            Document.source_url.label("source_url"),
            Document.updated_at.label("updated_at"),
            func.cast(0, Integer).label("rank"),
        )
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            Document.is_deleted.is_(False),
            DocumentChunk.role == "child",
            DocumentChunk.is_current.is_(True),
        )
    )
    stmt = _apply_filters(stmt, filters)
    rows = (
        await db.execute(
            stmt.order_by(Document.updated_at.desc(), DocumentChunk.order_index.asc())
        )
    ).all()
    best_rows = _best_document_rows(rows, limit=limit, offset=offset)
    hits = await _build_hits(db, best_rows, [])
    return SearchResult(
        query="",
        backend="filters",
        total=len({row.document_id for row in rows}),
        hits=hits,
        limit=limit,
        offset=offset,
        filters=filters,
        retrieval={
            "mode": "filters",
            "vector_used": False,
            "degraded_reason": None,
            "active_profile_id": None,
        },
    )


def _detect_backend(db: AsyncSession) -> str:
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        return "postgresql"
    return "sqlite"


def _contains_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value))


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _best_document_rows(rows, *, limit: int, offset: int):
    """Keep the highest-ranked chunk per document, then paginate documents."""

    best = []
    seen: set[int] = set()
    for row in rows:
        if row.document_id in seen:
            continue
        seen.add(row.document_id)
        best.append(row)
    return best[offset : offset + limit]


async def _search_postgres(
    db: AsyncSession,
    *,
    query: str,
    terms: list[str],
    limit: int,
    offset: int,
    filters: dict,
) -> SearchResult:
    backend = "postgresql"
    ts_query = func.websearch_to_tsquery("simple", query)
    ts_vector: ColumnElement[Any] = func.to_tsvector(
        "simple",
        func.coalesce(DocumentChunk.search_text, ""),
    )
    fts_match: ColumnElement[Any] = ts_vector.op("@@")(ts_query)
    exact_match = DocumentChunk.search_text.ilike(_like_pattern(query), escape="\\")
    # PostgreSQL's built-in ``simple`` dictionary does not segment
    # continuous Chinese text. Keep FTS for Latin terms and ranking,
    # and add exact substring matching for CJK queries.
    match_clause: ColumnElement[Any] = (
        or_(fts_match, exact_match) if _contains_cjk(query) else fts_match
    )
    rank = (
        func.ts_rank_cd(ts_vector, ts_query) + case((exact_match, 1.0), else_=0.0)
    ).label("rank")

    base = (
        select(
            DocumentChunk.id.label("chunk_id"),
            DocumentChunk.parent_id.label("parent_id"),
            DocumentChunk.document_id.label("document_id"),
            DocumentChunk.document_version_id.label("document_version_id"),
            DocumentChunk.chunk_type.label("chunk_type"),
            DocumentChunk.heading_path.label("heading_path"),
            DocumentChunk.page.label("page"),
            DocumentChunk.paragraph_index.label("paragraph_index"),
            DocumentChunk.source_start.label("source_start"),
            DocumentChunk.source_end.label("source_end"),
            DocumentChunk.content.label("content"),
            DocumentChunk.extra.label("chunk_extra"),
            Document.title.label("title"),
            Document.source_type.label("source_type"),
            Document.source_url.label("source_url"),
            Document.updated_at.label("updated_at"),
            rank,
        )
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            Document.is_deleted.is_(False),
            DocumentChunk.role == "child",
            match_clause,
        )
    )

    base = _apply_filters(base, filters)
    base = base.order_by(rank.desc(), DocumentChunk.id.asc())

    matching_rows = (await db.execute(base)).all()
    total = await _count_matches(
        db,
        match_clause=match_clause,
        filters=filters,
    )
    rows = _best_document_rows(matching_rows, limit=limit, offset=offset)

    hits = await _build_hits(db, rows, terms)
    return SearchResult(
        query=query,
        backend=backend,
        total=total,
        hits=hits,
        limit=limit,
        offset=offset,
        filters=filters,
    )


async def _search_like(
    db: AsyncSession,
    *,
    query: str,
    terms: list[str],
    limit: int,
    offset: int,
    filters: dict,
) -> SearchResult:
    backend = "sqlite"
    like_clauses = [
        func.lower(DocumentChunk.search_text).like(
            _like_pattern(term.lower()), escape="\\"
        )
        for term in terms
    ]
    if not like_clauses:
        like_clauses = [
            func.lower(DocumentChunk.search_text).like(
                _like_pattern(query.lower()), escape="\\"
            )
        ]

    match_clause = or_(*like_clauses)

    score_expr = func.coalesce(
        func.sum(func.length(func.coalesce(DocumentChunk.content, ""))),
        0,
    )

    subquery = (
        select(
            DocumentChunk.id.label("chunk_id"),
            score_expr.label("score"),
        )
        .where(
            DocumentChunk.role == "child",
            match_clause,
        )
        .group_by(DocumentChunk.id)
        .subquery()
    )

    base = (
        select(
            DocumentChunk.id.label("chunk_id"),
            DocumentChunk.parent_id.label("parent_id"),
            DocumentChunk.document_id.label("document_id"),
            DocumentChunk.document_version_id.label("document_version_id"),
            DocumentChunk.chunk_type.label("chunk_type"),
            DocumentChunk.heading_path.label("heading_path"),
            DocumentChunk.page.label("page"),
            DocumentChunk.paragraph_index.label("paragraph_index"),
            DocumentChunk.source_start.label("source_start"),
            DocumentChunk.source_end.label("source_end"),
            DocumentChunk.content.label("content"),
            DocumentChunk.extra.label("chunk_extra"),
            Document.title.label("title"),
            Document.source_type.label("source_type"),
            Document.source_url.label("source_url"),
            Document.updated_at.label("updated_at"),
            subquery.c.score.label("rank"),
        )
        .join(subquery, subquery.c.chunk_id == DocumentChunk.id)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.is_deleted.is_(False))
    )

    base = _apply_filters(base, filters)
    base = base.order_by(subquery.c.score.desc(), DocumentChunk.id.asc())

    matching_rows = (await db.execute(base)).all()
    total = await _count_matches_like(db, like_clauses, filters)
    rows = _best_document_rows(matching_rows, limit=limit, offset=offset)

    hits = await _build_hits(db, rows, terms)
    return SearchResult(
        query=query,
        backend=backend,
        total=total,
        hits=hits,
        limit=limit,
        offset=offset,
        filters=filters,
    )


def _apply_filters(stmt, filters: dict):
    conditions = []
    if filters.get("matches_none"):
        conditions.append(false())
    category_ids = filters.get("category_ids") or []
    category_slugs = filters.get("category_slugs") or []
    if category_ids or category_slugs:
        sub = select(DocumentCategory.document_id)
        if category_ids:
            sub = sub.where(DocumentCategory.category_id.in_(category_ids))
        if category_slugs:
            sub = sub.join(Category, Category.id == DocumentCategory.category_id).where(
                Category.slug.in_(category_slugs)
            )
        conditions.append(Document.id.in_(sub))
    tag_ids = filters.get("tag_ids") or []
    tag_slugs = filters.get("tag_slugs") or []
    if tag_ids or tag_slugs:
        sub = select(DocumentTag.document_id)
        if tag_ids:
            sub = sub.where(DocumentTag.tag_id.in_(tag_ids))
        if tag_slugs:
            sub = sub.join(Tag, Tag.id == DocumentTag.tag_id).where(
                Tag.slug.in_(tag_slugs)
            )
        conditions.append(Document.id.in_(sub))
    source_types = filters.get("source_types") or []
    if source_types:
        conditions.append(Document.source_type.in_(source_types))
    document_ids = filters.get("document_ids") or []
    if document_ids:
        conditions.append(Document.id.in_(document_ids))
    if conditions:
        stmt = stmt.where(and_(*conditions))
    return stmt


async def _count_matches(
    db: AsyncSession,
    *,
    match_clause,
    filters: dict,
) -> int:
    count_stmt = (
        select(func.count(func.distinct(DocumentChunk.document_id)))
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            Document.is_deleted.is_(False),
            DocumentChunk.role == "child",
            match_clause,
        )
    )
    count_stmt = _apply_filters(count_stmt, filters)
    return int((await db.execute(count_stmt)).scalar_one_or_none() or 0)


async def _count_matches_like(
    db: AsyncSession,
    like_clauses: Iterable,
    filters: dict,
) -> int:
    match_clause = or_(*like_clauses) if like_clauses else None
    count_stmt = (
        select(func.count(func.distinct(DocumentChunk.document_id)))
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.is_deleted.is_(False), DocumentChunk.role == "child")
    )
    if match_clause is not None:
        count_stmt = count_stmt.where(match_clause)
    count_stmt = _apply_filters(count_stmt, filters)
    return int((await db.execute(count_stmt)).scalar_one_or_none() or 0)


async def _build_hits(
    db: AsyncSession,
    rows,
    terms: list[str],
) -> list[SearchHit]:
    if not rows:
        return []
    document_ids = sorted({row.document_id for row in rows})
    categories_map, tags_map = await _load_taxonomy(db, document_ids)
    hits: list[SearchHit] = []
    for row in rows:
        content = row.content or ""
        snippet, highlights = _render_snippet(content, terms)
        heading_path = _heading_path(row.heading_path)
        hits.append(
            SearchHit(
                document_id=row.document_id,
                document_version_id=row.document_version_id,
                title=row.title,
                source_type=_source_type_label(row.source_type),
                source_url=row.source_url,
                updated_at=row.updated_at,
                chunk_id=row.chunk_id,
                parent_id=row.parent_id,
                chunk_type=row.chunk_type,
                heading_path=heading_path,
                page=row.page,
                paragraph_index=row.paragraph_index,
                source_start=row.source_start,
                source_end=row.source_end,
                table_location=table_location_from_extra(row.chunk_extra),
                score=float(row.rank or 0.0),
                snippet=snippet,
                highlights=highlights,
                categories=categories_map.get(row.document_id, []),
                tags=tags_map.get(row.document_id, []),
            )
        )
    return hits


async def _load_taxonomy(
    db: AsyncSession, document_ids: list[int]
) -> tuple[dict[int, list[dict]], dict[int, list[dict]]]:
    if not document_ids:
        return {}, {}
    category_rows = (
        await db.execute(
            select(
                DocumentCategory.document_id, Category.id, Category.slug, Category.name
            )
            .join(Category, Category.id == DocumentCategory.category_id)
            .where(DocumentCategory.document_id.in_(document_ids))
            .order_by(DocumentCategory.id)
        )
    ).all()
    categories_map: dict[int, list[dict]] = {}
    for document_id, cat_id, slug, name in category_rows:
        categories_map.setdefault(document_id, []).append(
            {"id": cat_id, "slug": slug, "name": name}
        )

    tag_rows = (
        await db.execute(
            select(DocumentTag.document_id, Tag.id, Tag.slug, Tag.name)
            .join(Tag, Tag.id == DocumentTag.tag_id)
            .where(DocumentTag.document_id.in_(document_ids))
            .order_by(DocumentTag.id)
        )
    ).all()
    tags_map: dict[int, list[dict]] = {}
    for document_id, tag_id, slug, name in tag_rows:
        tags_map.setdefault(document_id, []).append(
            {"id": tag_id, "slug": slug, "name": name}
        )
    return categories_map, tags_map


def _heading_path(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, str) and value:
        return [value]
    return []


def _source_type_label(value) -> str:
    if isinstance(value, DocumentSourceType):
        return value.value
    return str(value or "")


def _render_snippet(content: str, terms: list[str]) -> tuple[str, list[dict]]:
    """Build a short snippet plus highlight offsets for the UI."""

    if not content:
        return "", []
    if not terms:
        snippet = content[:SNIPPET_MAX_LENGTH]
        if len(content) > SNIPPET_MAX_LENGTH:
            snippet += "…"
        return snippet, []

    lowered = content.lower()
    match_positions: list[tuple[int, int]] = []
    for term in terms:
        start = 0
        needle = term.lower()
        if not needle:
            continue
        while True:
            idx = lowered.find(needle, start)
            if idx == -1:
                break
            match_positions.append((idx, idx + len(needle)))
            start = idx + len(needle) or idx + 1
    if not match_positions:
        snippet = content[:SNIPPET_MAX_LENGTH]
        if len(content) > SNIPPET_MAX_LENGTH:
            snippet += "…"
        return snippet, []

    match_positions.sort()
    first = match_positions[0][0]
    start = max(0, first - SNIPPET_RADIUS)
    end = min(len(content), start + SNIPPET_MAX_LENGTH)
    if end - start < SNIPPET_MAX_LENGTH and start > 0:
        start = max(0, end - SNIPPET_MAX_LENGTH)
    snippet = content[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(content):
        snippet = snippet + "…"
    highlights: list[dict] = []
    for begin, finish in match_positions:
        local_begin = begin - start
        local_end = finish - start
        if local_end <= 0 or local_begin >= len(snippet):
            continue
        local_begin = max(0, local_begin)
        local_end = min(len(snippet), local_end)
        highlights.append({"start": local_begin, "end": local_end})
    return snippet, highlights
