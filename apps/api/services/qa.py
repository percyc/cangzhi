"""Knowledge-base Q&A service (M3-2 first cut).

The Q&A service is intentionally narrow:

* It runs an FTS-style retrieval over ``document_chunks`` (the same
  source as :mod:`apps.api.services.search`). It does not yet combine
  with embeddings, pgvector or reranking; that work is tracked in
  ``docs/ROADMAP.md`` under M3 follow-ups.
* The retrieved chunks are converted into evidence dicts with a stable
  integer id and a snippet that is bounded so the prompt never gets
  unbounded user-controlled text. The model only ever sees these
  snippets and the ``[id]`` markers; we never feed it raw user data
  that could exceed the token budget.
* Citations returned by the model are cross-checked against the
  evidence whitelist before they reach the user; fabricated or
  duplicated ids are rejected and the API returns a stable
  ``insufficient_evidence`` payload rather than a 500.

The service is API-shaped: ``ask`` returns a dataclass that the FastAPI
layer can render as JSON, including the filtered evidence list so the
front end can render deep links.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, case, false, func, or_, select
from sqlalchemy.exc import ProgrammingError

logger = logging.getLogger(__name__)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from ..ai import AIProvider, AIProviderError
from ..models.chunks import DocumentChunk
from ..models.documents import Document, DocumentSourceType, DocumentVersion
from .search import extract_cjk_ngrams, normalize_query, table_location_from_extra
from .structured_table import (
    TableQueryResult,
    candidate_dataset_document_ids,
    is_structured_table_question,
    try_structured_table_query,
)

# ---- Limits ---------------------------------------------------------------
# These constants cap what a single ask call can do. The point is to
# keep the prompt bounded even when the user passes a long question or
# the corpus is large, and to keep latency predictable.

MAX_QUESTION_LENGTH = 500
MAX_EVIDENCE_ITEMS = 8
EVIDENCE_SNIPPET_CHARS = 600
EVIDENCE_TOTAL_CHARS = 4_000
NEIGHBOR_CONTEXT_CHARS = 180
CJK_RECALL_LIMIT = MAX_EVIDENCE_ITEMS * 4
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_QUESTION_GRAMS = {
    "什么",
    "怎么",
    "如何",
    "为何",
    "是否",
    "哪些",
    "一下",
    "这个",
    "那个",
    "可以",
}


def _contains_cjk(value: str) -> bool:
    return bool(_CJK_RE.search(value))


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


# ---- Public dataclasses ---------------------------------------------------


@dataclass
class AskRequest:
    question: str
    category_ids: list[int] = field(default_factory=list)
    category_slugs: list[str] = field(default_factory=list)
    tag_ids: list[int] = field(default_factory=list)
    tag_slugs: list[str] = field(default_factory=list)
    source_types: list[str] = field(default_factory=list)
    document_ids: list[int] = field(default_factory=list)
    connector_ids: list[int] = field(default_factory=list)
    matches_none: bool = False


@dataclass
class Evidence:
    id: int
    document_id: int
    document_version_id: int
    chunk_id: int
    title: str
    heading_path: list[str]
    page: int | None
    paragraph_index: int | None
    source_start: int | None
    source_end: int | None
    snippet: str
    score: float
    table_location: dict = field(default_factory=dict)
    source_type: str = ""
    source_url: str | None = None
    categories: list[dict] = field(default_factory=list)
    tags: list[dict] = field(default_factory=list)

    def to_provider_dict(self) -> dict:
        payload = {
            "id": self.id,
            "title": self.title,
            "heading_path": list(self.heading_path),
            "snippet": self.snippet,
        }
        if self.table_location:
            payload["table_location"] = dict(self.table_location)
        return payload

    def to_citation(self) -> dict:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "chunk_id": self.chunk_id,
            "title": self.title,
            "heading_path": list(self.heading_path),
            "page": self.page,
            "paragraph_index": self.paragraph_index,
            "source_start": self.source_start,
            "source_end": self.source_end,
            "table_location": dict(self.table_location),
            "source_type": self.source_type,
            "source_url": self.source_url,
            "snippet": self.snippet,
            "categories": list(self.categories),
            "tags": list(self.tags),
        }


@dataclass
class AskResult:
    question: str
    answer: str
    insufficient_evidence: bool
    citations: list[dict]
    evidence: list[Evidence]
    provider: str
    model: str | None
    retrieval: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "insufficient_evidence": self.insufficient_evidence,
            "citations": self.citations,
            "evidence": [ev.to_citation() for ev in self.evidence],
            "provider": self.provider,
            "model": self.model,
            "retrieval": dict(self.retrieval),
        }


# ---- Errors ---------------------------------------------------------------


class AskError(RuntimeError):
    """Raised when a call to ``ask`` cannot be completed safely.

    The API layer maps this to a stable HTTP error code (502 by
    default) and a user-friendly message. The original message is kept
    short and never includes API keys, prompts or evidence content.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---- Service --------------------------------------------------------------


class QAService:
    """Build evidence and orchestrate the model call.

    The class does not own an ``AsyncSession``; one is passed per call
    so a single instance is safe to share across requests. The
    provider is also injected to keep the service unit-testable.
    """

    def __init__(self, provider: AIProvider | None) -> None:
        self._provider = provider

    @property
    def provider(self) -> AIProvider | None:
        return self._provider

    @property
    def is_provider_configured(self) -> bool:
        return self._provider is not None and self._provider.is_configured()

    async def ask(self, db: AsyncSession, request: AskRequest) -> AskResult:
        question = (request.question or "").strip()
        if not question:
            raise AskError("empty_question", "问题不能为空")
        if len(question) > MAX_QUESTION_LENGTH:
            raise AskError(
                "question_too_long",
                f"问题长度不能超过 {MAX_QUESTION_LENGTH} 字",
            )

        if not self.is_provider_configured:
            raise AskError(
                "provider_not_configured",
                "尚未配置问答模型，请先完成模型设置",
            )

        evidence, retrieval = await self.collect_evidence(
            db,
            question=question,
            category_ids=request.category_ids,
            category_slugs=request.category_slugs,
            tag_ids=request.tag_ids,
            tag_slugs=request.tag_slugs,
            source_types=request.source_types,
            document_ids=request.document_ids,
            connector_ids=request.connector_ids,
            matches_none=request.matches_none,
        )
        assert self._provider is not None  # checked above
        table_matches_none = request.matches_none
        candidate_table_document_ids = (
            [] if table_matches_none else list(request.document_ids)
        )
        if request.connector_ids and not table_matches_none:
            from .knowledge_scopes import resolve_connector_document_ids

            connector_document_ids = await resolve_connector_document_ids(
                db, request.connector_ids
            )
            if not connector_document_ids:
                table_matches_none = True
                candidate_table_document_ids = []
            elif candidate_table_document_ids:
                candidate_table_document_ids = sorted(
                    set(candidate_table_document_ids).intersection(
                        connector_document_ids
                    )
                )
                table_matches_none = not candidate_table_document_ids
            else:
                candidate_table_document_ids = connector_document_ids
        if not candidate_table_document_ids:
            candidate_table_document_ids = list(
                dict.fromkeys(
                    item.document_id for item in evidence if item.table_location
                )
            )
        if (
            not candidate_table_document_ids
            and is_structured_table_question(question)
            and not table_matches_none
        ):
            candidate_table_document_ids = await candidate_dataset_document_ids(
                db,
                filters={
                    "category_ids": list(request.category_ids),
                    "category_slugs": list(request.category_slugs),
                    "tag_ids": list(request.tag_ids),
                    "tag_slugs": list(request.tag_slugs),
                    "source_types": list(request.source_types),
                    "document_ids": list(request.document_ids),
                    "matches_none": request.matches_none,
                },
            )
        from .dataset_execution import DatasetExecutionError

        try:
            structured_result = await try_structured_table_query(
                db,
                provider=self._provider,
                question=question,
                document_ids=candidate_table_document_ids,
            )
        except DatasetExecutionError as exc:
            # Dataset execution is an optimisation over the normal evidence
            # path. A corrupt/missing artifact must not turn an otherwise
            # answerable knowledge question into a 500 response.
            logger.warning(
                "structured dataset query degraded to semantic retrieval: %s", exc
            )
            structured_result = None
        if structured_result is not None:
            structured_evidence = await _structured_result_to_evidence(
                db, structured_result
            )
            if structured_evidence is not None:
                evidence = [structured_evidence, *evidence[: MAX_EVIDENCE_ITEMS - 1]]
                for evidence_id, item in enumerate(evidence, start=1):
                    item.id = evidence_id
                retrieval = {
                    **retrieval,
                    "mode": "structured_table",
                    "structured_table": {
                        "document_id": structured_result.dataset.document_id,
                        "sheet_name": structured_result.dataset.sheet_name,
                        "region_index": structured_result.dataset.region_index,
                        "matched_rows": structured_result.matched_row_count,
                        "metric": structured_result.plan.metric,
                        "metric_column": structured_result.plan.metric_column,
                        "group_by": list(structured_result.plan.group_by),
                        "warnings": list(structured_result.warnings),
                    },
                }
        if not evidence:
            return AskResult(
                question=question,
                answer=(
                    "在当前知识库中没有找到与问题相关的资料。"
                    "可以尝试调整关键词、清空筛选，或先收藏相关文章后再提问。"
                ),
                insufficient_evidence=True,
                citations=[],
                evidence=[],
                provider=self._provider.name if self._provider else "",
                model=_model_name(self._provider),
                retrieval=retrieval,
            )

        provider_payload = [ev.to_provider_dict() for ev in evidence]
        try:
            assert self._provider is not None  # checked above
            answer = await asyncio.to_thread(
                self._provider.answer_question,
                question=question,
                evidence=provider_payload,
            )
        except AIProviderError as exc:
            raise AskError("provider_failed", "模型暂时不可用，请稍后再试") from exc

        evidence_by_id = {ev.id: ev for ev in evidence}
        # The provider's own ``validate_against_evidence`` already
        # rejects fabricated ids, but we double-check here so a future
        # provider that forgets the whitelist still cannot smuggle
        # bogus references to the user.
        unknown_ids = [cid for cid in answer.citation_ids if cid not in evidence_by_id]
        if unknown_ids:
            raise AskError(
                "provider_failed",
                "模型引用了不存在的证据，已被拒绝",
            )
        if not answer.insufficient_evidence and not answer.citation_ids:
            return AskResult(
                question=question,
                answer=(
                    "模型未能引用知识库中的资料，因此未提供答案。"
                    "可以尝试补充资料或换一个更具体的关键词。"
                ),
                insufficient_evidence=True,
                citations=[],
                evidence=[ev for ev in evidence],
                provider=self._provider.name if self._provider else "",
                model=_model_name(self._provider),
                retrieval=retrieval,
            )

        citations: list[dict] = [
            evidence_by_id[cid].to_citation() for cid in answer.citation_ids
        ]
        answer_text = answer.answer
        if structured_result is not None and not answer.insufficient_evidence:
            answer_text = _ensure_complete_structured_answer(
                answer_text,
                structured_result,
            )

        return AskResult(
            question=question,
            answer=answer_text,
            insufficient_evidence=answer.insufficient_evidence,
            citations=citations,
            evidence=evidence,
            provider=self._provider.name if self._provider else "",
            model=_model_name(self._provider),
            retrieval=retrieval,
        )

    # ---- evidence retrieval ------------------------------------------------

    async def collect_evidence(
        self,
        db: AsyncSession,
        *,
        question: str,
        category_ids: Sequence[int],
        category_slugs: Sequence[str],
        tag_ids: Sequence[int],
        tag_slugs: Sequence[str],
        source_types: Sequence[str],
        document_ids: Sequence[int] | None = None,
        connector_ids: Sequence[int] | None = None,
        matches_none: bool = False,
    ) -> tuple[list[Evidence], dict]:
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
        filters = {
            "category_ids": list(category_ids),
            "category_slugs": list(category_slugs),
            "tag_ids": list(tag_ids),
            "tag_slugs": list(tag_slugs),
            "source_types": list(source_types),
            "document_ids": effective_document_ids,
            "connector_ids": list(connector_ids or []),
            "matches_none": matches_none,
        }
        if _contains_cjk(question):
            rows = await self._recall_cjk(
                db,
                question=question,
                filters=filters,
                limit=CJK_RECALL_LIMIT,
            )
            if not rows:
                rows = (
                    await self._recall_postgres(
                        db,
                        question=question,
                        filters=filters,
                        limit=MAX_EVIDENCE_ITEMS,
                    )
                    if _detect_backend(db) == "postgresql"
                    else []
                )
        else:
            rows = (
                await self._recall_postgres(
                    db,
                    question=question,
                    filters=filters,
                    limit=MAX_EVIDENCE_ITEMS,
                )
                if _detect_backend(db) == "postgresql"
                else await self._recall_like(
                    db,
                    question=question,
                    filters=filters,
                    limit=MAX_EVIDENCE_ITEMS,
                )
            )
        from .hybrid_retrieval import recall_vector_chunks, rrf_scores

        vector = await recall_vector_chunks(
            db,
            query=question,
            filters=filters,
            limit=CJK_RECALL_LIMIT,
        )
        if vector.status.vector_used:
            lexical_ids = [row.chunk_id for row in rows]
            vector_ids = [row.chunk_id for row in vector.rows]
            scores = rrf_scores(lexical_ids, vector_ids)
            row_by_chunk = {row.chunk_id: row for row in [*rows, *vector.rows]}
            ordered_ids = sorted(
                row_by_chunk,
                key=lambda chunk_id: (-scores.get(chunk_id, 0.0), chunk_id),
            )
            rows = [
                _row_with_rank(row_by_chunk[chunk_id], scores[chunk_id])
                for chunk_id in ordered_ids
            ]
        if not rows:
            return [], vector.status.to_dict()
        evidence = await self._rows_to_evidence(
            db,
            rows,
            question=question,
        )
        return evidence[:MAX_EVIDENCE_ITEMS], vector.status.to_dict()

    async def _recall_postgres(
        self,
        db: AsyncSession,
        *,
        question: str,
        filters: dict,
        limit: int,
    ) -> list:
        ts_query = func.websearch_to_tsquery("simple", question)
        ts_vector: ColumnElement[Any] = func.to_tsvector(
            "simple",
            func.coalesce(DocumentChunk.search_text, ""),
        )
        fts_match: ColumnElement[Any] = ts_vector.op("@@")(ts_query)
        exact_match = DocumentChunk.search_text.ilike(
            f"%{_escape_like(question)}%", escape="\\"
        )
        match_clause: ColumnElement[Any] = (
            or_(fts_match, exact_match) if _contains_cjk(question) else fts_match
        )
        rank = (
            func.ts_rank_cd(ts_vector, ts_query) + case((exact_match, 1.0), else_=0.0)
        ).label("rank")
        stmt = (
            select(
                DocumentChunk.id.label("chunk_id"),
                DocumentChunk.document_id.label("document_id"),
                DocumentChunk.document_version_id.label("document_version_id"),
                DocumentChunk.heading_path.label("heading_path"),
                DocumentChunk.page.label("page"),
                DocumentChunk.paragraph_index.label("paragraph_index"),
                DocumentChunk.source_start.label("source_start"),
                DocumentChunk.source_end.label("source_end"),
                DocumentChunk.content.label("content"),
                Document.title.label("title"),
                Document.source_type.label("source_type"),
                Document.source_url.label("source_url"),
                rank,
            )
            .join(
                DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                Document.is_deleted.is_(False),
                DocumentChunk.role == "child",
                DocumentChunk.is_current.is_(True),
                Document.current_version_id == DocumentChunk.document_version_id,
                match_clause,
            )
        )
        stmt = _apply_filters(stmt, filters)
        stmt = stmt.order_by(rank.desc(), DocumentChunk.id.asc()).limit(limit)
        try:
            result = (await db.execute(stmt)).all()
        except ProgrammingError:
            return []
        return result

    async def _recall_like(
        self,
        db: AsyncSession,
        *,
        question: str,
        filters: dict,
        limit: int,
    ) -> list:
        terms = normalize_query(question)
        if not terms:
            return []
        like_clauses = [
            func.lower(DocumentChunk.search_text).like(
                _like_pattern(term.lower()), escape="\\"
            )
            for term in terms
        ]
        if not like_clauses:
            return []
        match_clause = or_(*like_clauses)
        stmt = (
            select(
                DocumentChunk.id.label("chunk_id"),
                DocumentChunk.document_id.label("document_id"),
                DocumentChunk.document_version_id.label("document_version_id"),
                DocumentChunk.heading_path.label("heading_path"),
                DocumentChunk.page.label("page"),
                DocumentChunk.paragraph_index.label("paragraph_index"),
                DocumentChunk.source_start.label("source_start"),
                DocumentChunk.source_end.label("source_end"),
                DocumentChunk.content.label("content"),
                Document.title.label("title"),
                Document.source_type.label("source_type"),
                Document.source_url.label("source_url"),
                func.coalesce(
                    func.length(func.coalesce(DocumentChunk.content, "")), 0
                ).label("rank"),
            )
            .join(
                DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                Document.is_deleted.is_(False),
                DocumentChunk.role == "child",
                DocumentChunk.is_current.is_(True),
                Document.current_version_id == DocumentChunk.document_version_id,
                match_clause,
            )
        )
        stmt = _apply_filters(stmt, filters)
        stmt = stmt.order_by(
            func.length(func.coalesce(DocumentChunk.content, "")).asc(),
            DocumentChunk.id.asc(),
        ).limit(limit)
        return (await db.execute(stmt)).all()

    async def _recall_cjk(
        self,
        db: AsyncSession,
        *,
        question: str,
        filters: dict,
        limit: int,
    ) -> list:
        """CJK-friendly recall using normalized terms + n-grams.

        The goal is to surface at least one matching chunk for a
        natural-language Chinese question without requiring the exact
        sentence to appear in the document. We use the same LIKE
        patterns the search service already supports, so the SQLite
        test environment can exercise the same code path.
        """

        terms = normalize_query(question)
        trigrams = extract_cjk_ngrams(question, min_gram=3, max_gram=3, max_ngrams=12)
        bigrams = extract_cjk_ngrams(question, min_gram=2, max_gram=2, max_ngrams=12)
        patterns: list[tuple[str, int]] = []
        seen: set[str] = set()
        weighted_terms = (
            [(term, 5) for term in terms]
            + [(term, 2) for term in trigrams]
            + [(term, 1) for term in bigrams]
        )
        for raw, weight in weighted_terms:
            if not raw or raw in _QUESTION_GRAMS:
                continue
            if raw in seen:
                continue
            seen.add(raw)
            patterns.append((raw, weight))
        if not patterns:
            return []
        # Use a small number of patterns to keep the SQL portable
        # across PostgreSQL and SQLite; n-grams beyond 12 are still
        # useful for ranking but the OR list stays bounded.
        bounded = patterns[:16]
        like_clauses = [
            func.lower(DocumentChunk.search_text).like(
                _like_pattern(term.lower()), escape="\\"
            )
            for term, _weight in bounded
        ]
        match_clause = or_(*like_clauses)
        # Sum of per-pattern match indicators gives a coarse
        # "how many of our needles landed in this chunk" score. The
        # expressions are built explicitly so SQLite can bind them
        # (generator expressions inside func.sum do not bind).
        score_parts = [
            case(
                (
                    func.lower(DocumentChunk.search_text).like(
                        _like_pattern(term.lower()), escape="\\"
                    ),
                    weight,
                ),
                else_=0,
            )
            for term, weight in bounded
        ]
        if not score_parts:
            score_expr: ColumnElement[Any] = func.coalesce(
                func.length(func.coalesce(DocumentChunk.content, "")), 0
            ).label("rank")
        else:
            score_sum = score_parts[0]
            for part in score_parts[1:]:
                score_sum = score_sum + part
            score_expr = func.coalesce(score_sum, 0).label("rank")
        stmt = (
            select(
                DocumentChunk.id.label("chunk_id"),
                DocumentChunk.document_id.label("document_id"),
                DocumentChunk.document_version_id.label("document_version_id"),
                DocumentChunk.heading_path.label("heading_path"),
                DocumentChunk.page.label("page"),
                DocumentChunk.paragraph_index.label("paragraph_index"),
                DocumentChunk.source_start.label("source_start"),
                DocumentChunk.source_end.label("source_end"),
                DocumentChunk.content.label("content"),
                Document.title.label("title"),
                Document.source_type.label("source_type"),
                Document.source_url.label("source_url"),
                score_expr,
            )
            .join(
                DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                Document.is_deleted.is_(False),
                DocumentChunk.role == "child",
                DocumentChunk.is_current.is_(True),
                Document.current_version_id == DocumentChunk.document_version_id,
                match_clause,
                score_expr >= (1 if len(question) <= 4 else 2),
            )
        )
        stmt = _apply_filters(stmt, filters)
        stmt = stmt.order_by(
            score_expr.desc(),
            func.length(func.coalesce(DocumentChunk.content, "")).asc(),
            DocumentChunk.id.asc(),
        ).limit(limit)
        return (await db.execute(stmt)).all()

    async def _rows_to_evidence(
        self,
        db: AsyncSession,
        rows,
        *,
        question: str,
    ) -> list[Evidence]:
        if not rows:
            return []
        # The Q&A service uses a stable positive integer id for the
        # model, distinct from the chunk primary key. Re-issuing the
        # same id across one ask call is fine: the prompt is bounded
        # and the citation check uses the same set.
        document_ids = sorted({row.document_id for row in rows})
        categories_map, tags_map = await _load_taxonomy(db, document_ids)
        chunk_ids = [row.chunk_id for row in rows]
        chunks = {
            chunk.id: chunk
            for chunk in (
                await db.execute(
                    select(DocumentChunk).where(DocumentChunk.id.in_(chunk_ids))
                )
            )
            .scalars()
            .all()
        }
        terms = normalize_query(question)

        evidence: list[Evidence] = []
        budget = EVIDENCE_TOTAL_CHARS
        for index, row in enumerate(rows, start=1):
            if budget <= 0:
                break
            content = await _expanded_chunk_context(
                db,
                chunks.get(row.chunk_id),
                fallback=row.content or "",
            )
            snippet = _build_snippet(content, terms=terms)
            chunk = chunks.get(row.chunk_id)
            if len(snippet) > EVIDENCE_SNIPPET_CHARS:
                snippet = snippet[:EVIDENCE_SNIPPET_CHARS] + "…"
            if len(snippet) > budget:
                snippet = snippet[:budget]
            budget -= len(snippet)
            evidence.append(
                Evidence(
                    id=index,
                    document_id=row.document_id,
                    document_version_id=row.document_version_id,
                    chunk_id=row.chunk_id,
                    title=row.title,
                    heading_path=_heading_path(row.heading_path),
                    page=row.page,
                    paragraph_index=row.paragraph_index,
                    source_start=row.source_start,
                    source_end=row.source_end,
                    table_location=(
                        table_location_from_extra(chunk.extra) if chunk else {}
                    ),
                    snippet=snippet,
                    score=float(row.rank or 0.0),
                    source_type=_source_type_label(row.source_type),
                    source_url=row.source_url,
                    categories=categories_map.get(row.document_id, []),
                    tags=tags_map.get(row.document_id, []),
                )
            )
        return evidence


# ---- helpers --------------------------------------------------------------


def _row_with_rank(row, rank: float):
    """Copy a SQLAlchemy row while replacing its retrieval score."""

    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        values = dict(mapping)
    else:
        values = dict(vars(row))
    values["rank"] = rank
    return SimpleNamespace(**values)


def _build_snippet(content: str, *, terms: list[str] | None) -> str:
    if not content:
        return ""
    if not terms:
        return content.strip()
    lowered = content.lower()
    positions: list[int] = []
    for term in terms:
        needle = term.lower()
        if not needle:
            continue
        idx = lowered.find(needle)
        if idx != -1:
            positions.append(idx)
    if not positions:
        return content.strip()
    first = min(positions)
    start = max(0, first - 80)
    end = min(len(content), start + EVIDENCE_SNIPPET_CHARS)
    snippet = content[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(content):
        snippet = snippet + "…"
    return snippet


async def _structured_result_to_evidence(
    db: AsyncSession,
    result: TableQueryResult,
) -> Evidence | None:
    rows = (
        await db.execute(
            select(
                DocumentChunk,
                Document.source_type,
                Document.source_url,
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.document_id == result.dataset.document_id,
                DocumentChunk.document_version_id == result.dataset.document_version_id,
                DocumentChunk.role == "child",
                DocumentChunk.is_current.is_(True),
            )
            .order_by(DocumentChunk.order_index)
        )
    ).all()
    selected = None
    for chunk, source_type, source_url in rows:
        extra = chunk.extra or {}
        if (
            extra.get("sheet_name") == result.dataset.sheet_name
            and int(extra.get("region_index") or 1) == result.dataset.region_index
            and (
                result.source_row_start is None
                or int(extra.get("row_end") or 0) >= result.source_row_start
            )
        ):
            selected = (chunk, source_type, source_url)
            break
    if selected is None and rows:
        selected = rows[0]
    if selected is None:
        return None
    chunk, source_type, source_url = selected
    location = {
        "sheet_name": result.dataset.sheet_name,
        "region_index": result.dataset.region_index,
        "row_start": result.source_row_start,
        "row_end": result.source_row_end,
        "column_names": list(result.dataset.columns),
    }
    return Evidence(
        id=1,
        document_id=result.dataset.document_id,
        document_version_id=result.dataset.document_version_id,
        chunk_id=chunk.id,
        title=result.dataset.title,
        heading_path=[
            result.dataset.sheet_name,
            f"数据区域 {result.dataset.region_index}",
            "精确计算结果",
        ],
        page=None,
        paragraph_index=chunk.paragraph_index,
        source_start=chunk.source_start,
        source_end=chunk.source_end,
        table_location=location,
        snippet=_structured_provider_summary(result),
        score=10.0,
        source_type=_source_type_label(source_type),
        source_url=source_url,
    )


def _structured_provider_summary(
    result: TableQueryResult,
    *,
    max_chars: int = 3_500,
) -> str:
    """Keep every exact detail row visible without sending redundant cells.

    Parsed spreadsheets often include a synthetic ``content`` cell which
    repeats every semantic column. Pretty-printed full rows can therefore hit
    the evidence limit halfway through a valid result. For detail queries we
    remove filter/constant/redundant columns, then progressively retain the
    most discriminating columns. The underlying result remains unchanged.
    """

    if result.plan.metric != "rows" or not result.rows:
        return result.summary[:max_chars]

    filter_columns = {item.column for item in result.plan.filters}
    ordered_columns = [
        column
        for column in result.dataset.columns
        if column not in filter_columns and column != "content"
    ]
    values_by_column = {
        column: [row.get(column) for row in result.rows] for column in ordered_columns
    }
    varying_columns = [
        column
        for column in ordered_columns
        if len({str(value) for value in values_by_column[column]}) > 1
    ]
    selected_columns = varying_columns or ordered_columns

    def render(columns: list[str], cell_chars: int = 120) -> str:
        detail_rows = [
            {
                column: (
                    str(row.get(column))[:cell_chars]
                    if row.get(column) is not None
                    else None
                )
                for column in columns
            }
            for row in result.rows
        ]
        payload = {
            "数据集": (
                f"{result.dataset.title} / {result.dataset.sheet_name} / "
                f"区域 {result.dataset.region_index}"
            ),
            "筛选条件": [
                {
                    "列": item.column,
                    "运算符": item.operator,
                    "值": item.value,
                }
                for item in result.plan.filters
            ],
            "精确命中行数": result.matched_row_count,
            "完整明细": detail_rows,
            "完整性要求": "回答必须覆盖上述每一行，不得省略。",
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    summary = render(selected_columns)
    while len(summary) > max_chars and len(selected_columns) > 1:

        def column_score(column: str) -> tuple[float, float]:
            values = [
                str(value)
                for value in values_by_column[column]
                if value not in (None, "")
            ]
            distinct_ratio = len(set(values)) / max(len(values), 1)
            average_length = sum(len(value) for value in values) / max(len(values), 1)
            return distinct_ratio, -average_length

        least_useful = min(selected_columns, key=column_score)
        selected_columns.remove(least_useful)
        summary = render(selected_columns)
    if len(summary) > max_chars:
        summary = render(selected_columns, cell_chars=40)
    if len(summary) > max_chars:
        identity_column = selected_columns[0]
        summary = json.dumps(
            {
                "筛选条件": [
                    [item.column[:80], item.operator, str(item.value)[:120]]
                    for item in result.plan.filters
                ],
                "精确命中行数": result.matched_row_count,
                "明细列": identity_column[:80],
                "完整明细值": [
                    str(row.get(identity_column))[:40] for row in result.rows
                ],
                "完整性要求": "回答必须覆盖上述每一项，不得省略。",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return summary


def _ensure_complete_structured_answer(
    answer: str,
    result: TableQueryResult,
) -> str:
    """Append any exact detail values omitted by the language model."""

    if result.plan.metric != "rows" or not result.rows:
        return answer
    filter_columns = {item.column for item in result.plan.filters}
    candidates = [
        column
        for column in result.dataset.columns
        if column not in filter_columns and column != "content"
    ]

    def candidate_values(column: str) -> list[str]:
        return [
            str(row.get(column)).strip()
            for row in result.rows
            if row.get(column) not in (None, "")
        ]

    def candidate_score(column: str) -> tuple[float, float, float]:
        values = candidate_values(column)
        if not values:
            return (0.0, 0.0, float("-inf"))
        distinct_ratio = len(set(values)) / len(values)
        nonnumeric_ratio = sum(
            1 for value in values if not value.replace(".", "", 1).isdigit()
        ) / len(values)
        average_length = sum(len(value) for value in values) / len(values)
        return (distinct_ratio, nonnumeric_ratio, -average_length)

    if not candidates:
        return answer
    identity_column = max(candidates, key=candidate_score)
    identity_values = list(dict.fromkeys(candidate_values(identity_column)))
    missing = [value for value in identity_values if value not in answer]
    if not missing:
        return answer
    suffix = "精确结果补充（模型未完整覆盖）：" + "、".join(missing)
    return f"{answer.rstrip()}\n\n{suffix}"


async def _expanded_chunk_context(
    db: AsyncSession,
    chunk: DocumentChunk | None,
    *,
    fallback: str,
) -> str:
    if chunk is None or chunk.parent_id is None:
        return fallback.strip()
    if (chunk.extra or {}).get("sheet_name") or (chunk.extra or {}).get("table_ranges"):
        # Spreadsheet rows are self-describing and must never receive a
        # character tail that starts or ends halfway through another row.
        return (chunk.content or fallback).strip()
    neighbors = (
        (
            await db.execute(
                select(DocumentChunk)
                .where(
                    DocumentChunk.parent_id == chunk.parent_id,
                    DocumentChunk.is_current.is_(True),
                    DocumentChunk.role == "child",
                    DocumentChunk.order_index.between(
                        chunk.order_index - 1,
                        chunk.order_index + 1,
                    ),
                )
                .order_by(DocumentChunk.order_index)
            )
        )
        .scalars()
        .all()
    )
    previous = next(
        (item.content for item in neighbors if item.order_index < chunk.order_index),
        "",
    )
    following = next(
        (item.content for item in neighbors if item.order_index > chunk.order_index),
        "",
    )
    return _merge_neighbor_context(
        previous,
        chunk.content or fallback,
        following,
    )


def _merge_neighbor_context(
    previous: str,
    current: str,
    following: str,
    *,
    flank_chars: int = NEIGHBOR_CONTEXT_CHARS,
) -> str:
    pieces = []
    if previous:
        pieces.append(previous[-flank_chars:].strip())
    pieces.append(current.strip())
    if following:
        pieces.append(following[:flank_chars].strip())
    merged = ""
    for piece in pieces:
        if not piece:
            continue
        merged = _merge_text_overlap(merged, piece)
    return merged


def _merge_text_overlap(left: str, right: str) -> str:
    if not left:
        return right
    max_overlap = min(len(left), len(right), NEIGHBOR_CONTEXT_CHARS + 2)
    for size in range(max_overlap, 0, -1):
        if left[-size:] == right[:size]:
            return left + right[size:]
    return f"{left}\n\n{right}"


def _detect_backend(db: AsyncSession) -> str:
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        return "postgresql"
    return "sqlite"


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_pattern(value: str) -> str:
    return f"%{_escape_like(value)}%"


def _apply_filters(stmt, filters: dict):
    from ..models.taxonomy import (
        Category,
        DocumentCategory,
        DocumentTag,
        Tag,
    )

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


async def _load_taxonomy(
    db: AsyncSession, document_ids: list[int]
) -> tuple[dict[int, list[dict]], dict[int, list[dict]]]:
    if not document_ids:
        return {}, {}
    from ..models.taxonomy import (
        Category,
        DocumentCategory,
        DocumentTag,
        Tag,
    )

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


def _model_name(provider: AIProvider | None) -> str | None:
    if provider is None:
        return None
    name = getattr(provider, "_model", None) or getattr(provider, "model", None)
    return str(name) if name else None
