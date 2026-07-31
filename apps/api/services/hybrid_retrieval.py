"""Hybrid lexical/vector retrieval for the active embedding profile.

The module keeps vector concerns out of the existing lexical search
implementation.  It generates exactly one query embedding, recalls
current child chunks from the active profile, and exposes RRF fusion
helpers shared by search and Q&A.  Any embedding-side failure is
reported as a degradation instead of interrupting keyword retrieval.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ..embeddings.compatibility import validate_embedding_vector
from ..models.auth import AIRuntimeConfig
from ..models.chunks import DocumentChunk
from ..models.documents import Document, DocumentVersion
from ..models.embedding_profiles import ChunkEmbedding, EmbeddingProfile
from ..security.secrets import (
    SecretDecryptError,
    SecretStoreError,
    decrypt_secret,
)
from .search import _apply_filters

VECTOR_CANDIDATE_LIMIT = 80
RRF_K = 60


@dataclass(frozen=True)
class RetrievalStatus:
    mode: str
    vector_used: bool
    degraded_reason: str | None = None
    active_profile_id: int | None = None

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "vector_used": self.vector_used,
            "degraded_reason": self.degraded_reason,
            "active_profile_id": self.active_profile_id,
        }


@dataclass(frozen=True)
class VectorRecall:
    rows: list
    status: RetrievalStatus


async def recall_vector_chunks(
    db: AsyncSession,
    *,
    query: str,
    filters: dict,
    limit: int = VECTOR_CANDIDATE_LIMIT,
) -> VectorRecall:
    """Recall vector-nearest current child chunks, or degrade safely."""

    config = (
        (
            await db.execute(
                select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc()).limit(1)
            )
        )
        .scalars()
        .first()
    )
    if config is None or config.active_embedding_profile_id is None:
        return _degraded("尚未启用向量索引")

    profile = await db.get(EmbeddingProfile, config.active_embedding_profile_id)
    if profile is None or profile.status != "active":
        return _degraded("当前向量索引不可用")

    try:
        runtime = _resolve_runtime(config, profile)
        vector = await _request_query_embedding(
            provider=runtime["provider"],
            base_url=runtime["base_url"],
            model=runtime["model"],
            api_key=runtime["api_key"],
            timeout_seconds=runtime["timeout_seconds"],
            text=query,
        )
        validate_embedding_vector(vector, dim=profile.dim)
        rows = await _recall_rows(
            db,
            profile_id=profile.id,
            query_vector=vector,
            filters=filters,
            limit=max(1, min(limit, VECTOR_CANDIDATE_LIMIT)),
        )
    except (
        ValueError,
        TypeError,
        httpx.HTTPError,
        SecretStoreError,
        SecretDecryptError,
        SQLAlchemyError,
    ) as exc:
        return _degraded(
            _public_failure_reason(exc),
            active_profile_id=profile.id,
        )

    return VectorRecall(
        rows=rows,
        status=RetrievalStatus(
            mode="hybrid",
            vector_used=True,
            active_profile_id=profile.id,
        ),
    )


def rrf_scores(*ranked_chunk_ids: Sequence[int], k: int = RRF_K) -> dict[int, float]:
    """Return deterministic reciprocal-rank-fusion scores by chunk id."""

    scores: dict[int, float] = {}
    for ranking in ranked_chunk_ids:
        seen: set[int] = set()
        for rank, chunk_id in enumerate(ranking, start=1):
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return scores


def _degraded(
    reason: str,
    *,
    active_profile_id: int | None = None,
) -> VectorRecall:
    return VectorRecall(
        rows=[],
        status=RetrievalStatus(
            mode="keyword",
            vector_used=False,
            degraded_reason=reason,
            active_profile_id=active_profile_id,
        ),
    )


def _resolve_runtime(
    config: AIRuntimeConfig,
    profile: EmbeddingProfile,
) -> dict:
    provider = (config.embedding_provider or "").strip()
    base_url = (config.embedding_base_url or "").strip().rstrip("/")
    model = (config.embedding_model or "").strip()
    if provider != profile.provider:
        raise ValueError("向量渠道与当前索引不一致")
    if base_url.lower() != (profile.base_url or "").strip().rstrip("/").lower():
        raise ValueError("向量服务地址与当前索引不一致")
    if model != (profile.model or "").strip():
        raise ValueError("向量模型与当前索引不一致")
    api_key = ""
    if provider == "openai":
        if not config.has_embedding_api_key or not config.embedding_api_key_cipher:
            raise ValueError("向量模型密钥未配置")
        api_key = decrypt_secret(config.embedding_api_key_cipher)
        if not api_key:
            raise ValueError("向量模型密钥不可用")
    if provider not in {"openai", "ollama"}:
        raise ValueError("向量渠道未启用")
    return {
        "provider": provider,
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "timeout_seconds": float(config.embedding_timeout_seconds or 30),
    }


async def _request_query_embedding(
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key: str,
    timeout_seconds: float,
    text: str,
) -> list[float]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if provider == "openai":
        headers["Authorization"] = f"Bearer {api_key}"
        url = f"{base_url}/embeddings"
        body = {"model": model, "input": text}
    else:
        url = f"{base_url}/api/embed"
        body = {"model": model, "input": text}
    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        trust_env=False,
    ) as client:
        response = await client.post(url, headers=headers, json=body)
    if response.status_code >= 400:
        raise ValueError(f"向量服务返回 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("向量服务响应格式错误") from exc
    if provider == "openai":
        data = payload.get("data") if isinstance(payload, dict) else None
        vector = data[0].get("embedding") if isinstance(data, list) and data else None
    else:
        embeddings = payload.get("embeddings") if isinstance(payload, dict) else None
        vector = embeddings[0] if isinstance(embeddings, list) and embeddings else None
        if vector is None and isinstance(payload, dict):
            vector = payload.get("embedding")
    if not isinstance(vector, list):
        raise TypeError("向量服务没有返回有效向量")
    return [float(value) for value in vector]


async def _recall_rows(
    db: AsyncSession,
    *,
    profile_id: int,
    query_vector: list[float],
    filters: dict,
    limit: int,
) -> list:
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        distance = ChunkEmbedding.vector.cosine_distance(query_vector)
        stmt = _vector_row_select((1.0 - distance).label("rank")).where(
            ChunkEmbedding.profile_id == profile_id
        )
        stmt = _apply_filters(stmt, filters)
        return (
            await db.execute(
                stmt.order_by(distance.asc(), DocumentChunk.id.asc()).limit(limit)
            )
        ).all()

    stmt = _vector_row_select(ChunkEmbedding.vector.label("stored_vector")).where(
        ChunkEmbedding.profile_id == profile_id
    )
    stmt = _apply_filters(stmt, filters)
    candidates = (await db.execute(stmt)).all()
    ranked = []
    for row in candidates:
        score = _cosine_similarity(query_vector, row.stored_vector)
        ranked.append(
            SimpleNamespace(
                **{
                    key: getattr(row, key)
                    for key in row._fields
                    if key != "stored_vector"
                },
                rank=score,
            )
        )
    ranked.sort(key=lambda row: (-row.rank, row.chunk_id))
    return ranked[:limit]


def _vector_row_select(score_column):
    return (
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
            score_column,
        )
        .join(DocumentChunk, DocumentChunk.id == ChunkEmbedding.chunk_id)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(
            Document.is_deleted.is_(False),
            DocumentChunk.role == "child",
            DocumentChunk.is_current.is_(True),
            Document.current_version_id == DocumentChunk.document_version_id,
        )
    )


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm == 0 or right_norm == 0:
        return -1.0
    return dot / (left_norm * right_norm)


def _public_failure_reason(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "向量服务请求超时，已自动使用关键词检索"
    if isinstance(exc, httpx.HTTPError):
        return "向量服务暂时无法连接，已自动使用关键词检索"
    message = str(exc)
    allowed = (
        "向量渠道",
        "向量服务地址",
        "向量模型",
        "向量服务返回",
        "向量服务没有",
    )
    if message.startswith(allowed):
        return f"{message}，已自动使用关键词检索"
    return "向量检索暂时不可用，已自动使用关键词检索"
