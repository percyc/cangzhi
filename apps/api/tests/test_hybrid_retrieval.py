"""Tests for M3-6c vector recall, RRF fusion and safe degradation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import apps.api.models  # noqa: F401
from apps.api.core.db import Base
from apps.api.models.auth import AIRuntimeConfig
from apps.api.models.chunks import DocumentChunk
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.embedding_profiles import ChunkEmbedding, EmbeddingProfile
from apps.api.services.hybrid_retrieval import rrf_scores
from apps.api.services.search import _filter_vector_only_rows, search_documents


def test_rrf_rewards_chunks_seen_by_both_retrievers():
    scores = rrf_scores([1, 2, 3], [3, 1, 4])
    assert scores[1] > scores[2]
    assert scores[3] > scores[4]
    assert len(scores) == 4


def test_weak_vector_only_rows_do_not_pollute_a_lexical_search():
    rows = [
        SimpleNamespace(chunk_id=1, rank=0.31),
        SimpleNamespace(chunk_id=2, rank=0.81),
        SimpleNamespace(chunk_id=3, rank=0.99),
    ]

    filtered = _filter_vector_only_rows(rows, lexical_chunk_ids=[3])

    assert [row.chunk_id for row in filtered] == [2, 3]


def test_vector_only_recall_is_preserved_when_lexical_search_is_empty():
    rows = [SimpleNamespace(chunk_id=1, rank=0.12)]
    assert _filter_vector_only_rows(rows, lexical_chunk_ids=[]) == rows


def test_vector_only_document_is_returned_by_hybrid_search(monkeypatch):
    async def scenario():
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            document = Document(
                title="语义匹配资料",
                source_type=DocumentSourceType.note,
            )
            db.add(document)
            await db.flush()
            version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                content_hash="v" * 64,
                raw_content="完全不同的字面内容",
                processing_status="ready",
            )
            db.add(version)
            await db.flush()
            document.current_version_id = version.id
            chunk = DocumentChunk(
                document_id=document.id,
                document_version_id=version.id,
                external_id="child-1",
                role="child",
                chunk_type="paragraph",
                order_index=0,
                content="完全不同的字面内容",
                search_text="完全不同的字面内容",
                content_hash="c" * 64,
                char_count=10,
                token_estimate=5,
                is_current=True,
            )
            db.add(chunk)
            await db.flush()
            profile = EmbeddingProfile(
                provider="ollama",
                base_url="http://ollama:11434",
                model="bge-m3",
                dim=3,
                has_api_key=False,
                config_fingerprint="f" * 64,
                status="active",
            )
            db.add(profile)
            await db.flush()
            db.add(
                ChunkEmbedding(
                    profile_id=profile.id,
                    chunk_id=chunk.id,
                    content_hash=chunk.content_hash,
                    vector=[1.0, 0.0, 0.0],
                )
            )
            db.add(
                AIRuntimeConfig(
                    singleton_key="singleton",
                    provider="disabled",
                    timeout_seconds=30,
                    prompt_version="v1",
                    embedding_provider="ollama",
                    embedding_base_url="http://ollama:11434",
                    embedding_model="bge-m3",
                    embedding_timeout_seconds=30,
                    active_embedding_profile_id=profile.id,
                )
            )
            await db.commit()

            async def fake_embedding(**_kwargs):
                return [1.0, 0.0, 0.0]

            monkeypatch.setattr(
                "apps.api.services.hybrid_retrieval._request_query_embedding",
                fake_embedding,
            )
            result = await search_documents(db, query="没有字面命中的概念")
            assert result.backend == "hybrid"
            assert result.retrieval["vector_used"] is True
            assert [hit.document_id for hit in result.hits] == [document.id]
        await engine.dispose()

    asyncio.run(scenario())


def test_search_degrades_when_query_embedding_fails(monkeypatch):
    async def scenario():
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            profile = EmbeddingProfile(
                provider="ollama",
                base_url="http://ollama:11434",
                model="bge-m3",
                dim=3,
                has_api_key=False,
                config_fingerprint="f" * 64,
                status="active",
            )
            db.add(profile)
            await db.flush()
            db.add(
                AIRuntimeConfig(
                    singleton_key="singleton",
                    provider="disabled",
                    timeout_seconds=30,
                    prompt_version="v1",
                    embedding_provider="ollama",
                    embedding_base_url="http://ollama:11434",
                    embedding_model="bge-m3",
                    active_embedding_profile_id=profile.id,
                )
            )
            await db.commit()

            async def failing_embedding(**_kwargs):
                raise ValueError("upstream secret response")

            monkeypatch.setattr(
                "apps.api.services.hybrid_retrieval._request_query_embedding",
                failing_embedding,
            )
            result = await search_documents(db, query="任意查询")
            assert result.backend == "sqlite"
            assert result.retrieval["vector_used"] is False
            assert "自动使用关键词检索" in result.retrieval["degraded_reason"]
            assert "secret" not in result.retrieval["degraded_reason"]
        await engine.dispose()

    asyncio.run(scenario())
