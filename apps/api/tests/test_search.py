"""Tests for the /api/search endpoints."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import apps.api.models  # noqa: F401 - ensure all tables register
from apps.api.core.db import Base, get_db
from apps.api.main import app
from apps.api.models.chunks import DocumentChunk
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.taxonomy import (
    Category,
    DocumentCategory,
    DocumentTag,
    Tag,
)
from apps.api.models.workspaces import Workspace
from apps.api.parsers.base import Block, StructuredContent
from apps.api.services.chunker import build_chunk_specs, chunk_content_hash


@pytest.fixture
def search_db(tmp_path):
    """Spin up a dedicated async SQLite engine and expose it to tests."""

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def prepare():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            session.add(
                Workspace(
                    slug="default",
                    name="默认空间",
                    is_default=True,
                    status="active",
                    settings={},
                )
            )
            await session.commit()

    asyncio.run(prepare())

    async def override_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    from apps.api.api.auth import require_admin

    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
        id=1, username="tester", is_active=True
    )
    yield session_factory

    async def dispose():
        await engine.dispose()

    asyncio.run(dispose())
    app.dependency_overrides.pop(get_db, None)
    from apps.api.api.auth import require_admin as _require_admin

    app.dependency_overrides.pop(_require_admin, None)


async def _seed_document(
    session_factory,
    *,
    title: str,
    source_type,
    body: str,
    category_slug: str | None = None,
    tag_slug: str | None = None,
    heading: str | None = None,
    paragraphs: list[str] | None = None,
):
    if paragraphs is None:
        paragraphs = [body]
    blocks: list[Block] = []
    if heading:
        blocks.append(
            Block(
                type="heading",
                text=heading,
                heading_path=[heading],
                level=1,
                paragraph_index=0,
            )
        )
    for index, paragraph in enumerate(paragraphs):
        blocks.append(
            Block(
                type="paragraph",
                text=paragraph,
                heading_path=[heading] if heading else [],
                paragraph_index=index,
            )
        )

    structured = StructuredContent(document_type="markdown", blocks=blocks)
    structured_dict = structured.to_dict()
    full_text = structured.full_text()

    async with session_factory() as session:
        document = Document(title=title, source_type=source_type)
        session.add(document)
        await session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash=chunk_content_hash(full_text),
            raw_content=full_text,
            structured_content=structured_dict,
            processing_status="ready",
        )
        session.add(version)
        await session.flush()

        if category_slug:
            category = await session.scalar(
                select(Category).where(Category.slug == category_slug)
            )
            if category is None:
                category = Category(
                    slug=category_slug,
                    name=category_slug.title(),
                    sort_order=10,
                )
                session.add(category)
                await session.flush()
            session.add(
                DocumentCategory(
                    document_id=document.id,
                    document_version_id=version.id,
                    category_id=category.id,
                    is_primary=True,
                    source="model",
                )
            )

        if tag_slug:
            tag = await session.scalar(select(Tag).where(Tag.slug == tag_slug))
            if tag is None:
                tag = Tag(slug=tag_slug, name=tag_slug)
                session.add(tag)
                await session.flush()
            session.add(
                DocumentTag(
                    document_id=document.id,
                    document_version_id=version.id,
                    tag_id=tag.id,
                    source="model",
                )
            )

        specs = build_chunk_specs(structured)
        parent_external_to_id: dict[str, int] = {}
        for spec in specs:
            search_text = "\n".join(
                [
                    title,
                    " > ".join(spec.heading_path or []),
                    spec.content,
                ]
            ).strip()
            chunk = DocumentChunk(
                document_id=document.id,
                document_version_id=version.id,
                parent_id=None,
                external_id=spec.external_id,
                role=spec.role,
                chunk_type=spec.chunk_type,
                order_index=spec.order_index,
                content=spec.content,
                search_text=search_text,
                content_hash=spec.content_hash,
                heading_path=list(spec.heading_path),
                page=spec.page,
                paragraph_index=spec.paragraph_index,
                source_start=spec.source_start,
                source_end=spec.source_end,
                char_count=spec.char_count,
                token_estimate=spec.token_estimate,
                language=spec.language,
                is_current=True,
            )
            session.add(chunk)
            await session.flush()
            if spec.role == "parent":
                parent_external_to_id[spec.external_id] = chunk.id
        for spec in specs:
            if spec.role != "child" or not spec.parent_external_id:
                continue
            parent_pk = parent_external_to_id.get(spec.parent_external_id)
            if parent_pk is None:
                continue
            child = await session.scalar(
                select(DocumentChunk).where(
                    DocumentChunk.document_version_id == version.id,
                    DocumentChunk.external_id == spec.external_id,
                )
            )
            if child is not None:
                child.parent_id = parent_pk
                session.add(child)
        await session.commit()
    return document.id


def test_search_returns_matching_document(search_db):
    from apps.api.services.search import search_documents

    async def _run():
        await _seed_document(
            search_db,
            title="藏知检索",
            source_type=DocumentSourceType.note,
            body="搜索可以找到相关片段。",
        )
        await _seed_document(
            search_db,
            title="无关文档",
            source_type=DocumentSourceType.note,
            body="完全不相关内容。",
        )
        async with search_db() as session:
            return await search_documents(session, query="搜索")

    result = asyncio.run(_run())
    assert result.backend == "sqlite"
    assert result.total == 1
    assert result.hits[0].title == "藏知检索"
    assert "搜索" in result.hits[0].snippet
    assert result.hits[0].highlights


def test_search_matches_title_only(search_db):
    from apps.api.services.search import search_documents

    async def _run():
        await _seed_document(
            search_db,
            title="只在标题出现的稀有词",
            source_type=DocumentSourceType.note,
            body="正文没有这个检索词。",
        )
        async with search_db() as session:
            return await search_documents(session, query="稀有词")

    result = asyncio.run(_run())
    assert result.total == 1
    assert result.hits[0].title == "只在标题出现的稀有词"


def test_search_returns_one_best_hit_per_document(search_db):
    from apps.api.services.search import search_documents

    async def _run():
        paragraph = "重复命中词。" + ("这是用于生成独立片段的正文。" * 45)
        await _seed_document(
            search_db,
            title="多片段资料",
            source_type=DocumentSourceType.note,
            body=paragraph,
            paragraphs=[paragraph, paragraph],
        )
        async with search_db() as session:
            return await search_documents(session, query="重复命中词")

    result = asyncio.run(_run())
    assert result.total == 1
    assert len(result.hits) == 1


def test_search_filters_by_category(search_db):
    from apps.api.services.search import search_documents

    async def _run():
        await _seed_document(
            search_db,
            title="技术文档",
            source_type=DocumentSourceType.file,
            body="讨论向量检索的实现细节。",
            category_slug="tech",
        )
        await _seed_document(
            search_db,
            title="生活笔记",
            source_type=DocumentSourceType.note,
            body="今天学做了一道新菜。",
            category_slug="life",
        )
        async with search_db() as session:
            tech = await search_documents(
                session, query="检索", category_slugs=["tech"]
            )
            life = await search_documents(
                session, query="检索", category_slugs=["life"]
            )
            return tech, life

    tech, life = asyncio.run(_run())
    assert tech.total == 1
    assert tech.hits[0].title == "技术文档"
    assert life.total == 0


def test_search_filters_by_source_type(search_db):
    from apps.api.services.search import search_documents

    async def _run():
        await _seed_document(
            search_db,
            title="随手记",
            source_type=DocumentSourceType.note,
            body="随手记录一些想法。",
        )
        await _seed_document(
            search_db,
            title="网页收藏",
            source_type=DocumentSourceType.url,
            body="网页摘录的关键内容。",
        )
        async with search_db() as session:
            return await search_documents(session, query="内容", source_types=["url"])

    result = asyncio.run(_run())
    assert result.total == 1
    assert result.hits[0].source_type == "url"


def test_search_filters_by_tag(search_db):
    from apps.api.services.search import search_documents

    async def _run():
        await _seed_document(
            search_db,
            title="带标签的笔记",
            source_type=DocumentSourceType.note,
            body="讨论全文检索技术。",
            tag_slug="search",
        )
        await _seed_document(
            search_db,
            title="没有标签",
            source_type=DocumentSourceType.note,
            body="讨论做饭心得。",
        )
        async with search_db() as session:
            return await search_documents(
                session, query="全文检索", tag_slugs=["search"]
            )

    result = asyncio.run(_run())
    assert result.total == 1
    assert result.hits[0].title == "带标签的笔记"


def test_search_document_scope_and_empty_intersection_never_expand(search_db):
    from apps.api.services.search import search_documents

    async def _run():
        selected_id = await _seed_document(
            search_db,
            title="选中的资料",
            source_type=DocumentSourceType.note,
            body="共同检索词只应命中选中的资料。",
        )
        await _seed_document(
            search_db,
            title="范围外资料",
            source_type=DocumentSourceType.note,
            body="共同检索词也存在于范围外。",
        )
        async with search_db() as session:
            selected = await search_documents(
                session, query="共同检索词", document_ids=[selected_id]
            )
            empty = await search_documents(
                session, query="共同检索词", matches_none=True
            )
            return selected, empty

    selected, empty = asyncio.run(_run())
    assert [hit.title for hit in selected.hits] == ["选中的资料"]
    assert empty.total == 0
    assert empty.hits == []


def test_search_post_endpoint(search_db):
    asyncio.run(
        _seed_document(
            search_db,
            title="搜索引擎",
            source_type=DocumentSourceType.note,
            body="讨论搜索引擎的全文索引实现。",
        )
    )
    client = TestClient(app)
    response = client.post("/api/search", json={"query": "全文索引"})
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "全文索引"
    assert body["total"] == 1
    assert body["hits"][0]["title"] == "搜索引擎"
    assert "全文索引" in body["hits"][0]["snippet"]


def test_search_get_endpoint(search_db):
    asyncio.run(
        _seed_document(
            search_db,
            title="链接笔记",
            source_type=DocumentSourceType.url,
            body="网页内容里出现了关键词。",
        )
    )
    client = TestClient(app)
    response = client.get("/api/search", params={"q": "关键词"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["hits"][0]["title"] == "链接笔记"


def test_search_filters_endpoint(search_db):
    asyncio.run(
        _seed_document(
            search_db,
            title="过滤测试",
            source_type=DocumentSourceType.note,
            body="包含若干词条。",
        )
    )
    client = TestClient(app)
    response = client.get("/api/search/filters")
    assert response.status_code == 200
    body = response.json()
    assert "source_types" in body
    assert {item["value"] for item in body["source_types"]} >= {
        "note",
        "url",
        "file",
    }


def test_search_allows_filter_without_query(search_db):
    asyncio.run(
        _seed_document(
            search_db,
            title="仅筛选链接",
            source_type=DocumentSourceType.url,
            body="无需输入关键词也应该出现。",
            category_slug="industry",
        )
    )
    asyncio.run(
        _seed_document(
            search_db,
            title="不应出现的笔记",
            source_type=DocumentSourceType.note,
            body="另一份资料。",
        )
    )
    client = TestClient(app)
    response = client.post(
        "/api/search",
        json={
            "query": "",
            "source_types": ["url"],
            "category_slugs": ["industry"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["backend"] == "filters"
    assert body["retrieval"]["mode"] == "filters"
    assert [hit["title"] for hit in body["hits"]] == ["仅筛选链接"]


def test_search_no_results(search_db):
    asyncio.run(
        _seed_document(
            search_db,
            title="空查询测试",
            source_type=DocumentSourceType.note,
            body="不相关。",
        )
    )
    client = TestClient(app)
    response = client.get("/api/search", params={"q": "完全不存在的字符串"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["hits"] == []


def test_search_treats_like_wildcards_as_literal(search_db):
    from apps.api.services.search import search_documents

    async def _run():
        await _seed_document(
            search_db,
            title="普通资料",
            source_type=DocumentSourceType.note,
            body="这里没有百分号。",
        )
        async with search_db() as session:
            return await search_documents(session, query="%")

    result = asyncio.run(_run())
    assert result.total == 0
