"""Tests for canonical knowledge-scope semantics."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from apps.api.core.db import Base
from apps.api.models.documents import Document, DocumentSourceType
from apps.api.models.knowledge_scopes import KnowledgeScope
from apps.api.models.webdav import WebDAVEntry, WebDAVSource
from apps.api.services.knowledge_scopes import (
    SYSTEM_SCOPE_FILES,
    SYSTEM_SCOPE_NOTES,
    SYSTEM_SCOPE_WEB,
    KnowledgeScopeError,
    KnowledgeScopeNotFound,
    KnowledgeScopeResolver,
    merge_filters,
    normalize_scope_filter,
)


@pytest.fixture
def scope_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def prepare():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(prepare())
    yield sessions
    asyncio.run(engine.dispose())


def test_normalize_scope_filter_is_strict_and_canonical():
    assert normalize_scope_filter(
        {
            "document_ids": [3, 1, 3],
            "source_types": ["NOTE", "file"],
            "tag_ids": [],
        }
    ) == {
        "document_ids": [1, 3],
        "source_types": ["file", "note"],
    }

    with pytest.raises(KnowledgeScopeError) as unknown:
        normalize_scope_filter({"query": "不能存进 scope"})
    assert unknown.value.code == "invalid_scope_filter"

    with pytest.raises(KnowledgeScopeError):
        normalize_scope_filter({"category_ids": ["1"]})
    with pytest.raises(KnowledgeScopeError):
        normalize_scope_filter({"source_types": ["email"]})


def test_merge_filters_intersects_same_dimension_and_combines_others():
    resolved = merge_filters(
        {
            "category_ids": [1, 2, 3],
            "tag_ids": [8],
            "source_types": ["file", "note"],
        },
        {
            "category_ids": [2, 3, 4],
            "document_ids": [10],
            "source_types": ["note"],
        },
    )
    assert resolved.category_ids == [2, 3]
    assert resolved.tag_ids == [8]
    assert resolved.document_ids == [10]
    assert resolved.source_types == ["note"]
    assert resolved.matches_none is False


def test_empty_intersection_is_explicitly_no_match():
    resolved = merge_filters(
        {"category_ids": [1, 2]},
        {"category_ids": [3, 4]},
    )
    assert resolved.category_ids == []
    assert resolved.matches_none is True
    assert resolved.to_filters_dict()["matches_none"] is True


@pytest.mark.asyncio
async def test_system_scopes_are_code_defined(scope_db):
    async with scope_db() as session:
        resolver = KnowledgeScopeResolver(session)
        assert (
            await resolver.resolve(scope_slug=SYSTEM_SCOPE_NOTES)
        ).source_types == ["note"]
        assert (
            await resolver.resolve(scope_slug=SYSTEM_SCOPE_WEB)
        ).source_types == ["url"]
        assert (
            await resolver.resolve(scope_slug=SYSTEM_SCOPE_FILES)
        ).source_types == ["file"]


@pytest.mark.asyncio
async def test_missing_saved_scope_never_falls_back_to_all(scope_db):
    async with scope_db() as session:
        resolver = KnowledgeScopeResolver(session)
        with pytest.raises(KnowledgeScopeNotFound):
            await resolver.resolve(scope_slug="missing")
        with pytest.raises(KnowledgeScopeError) as conflict:
            await resolver.resolve(scope_id=1, scope_slug="saved")
        assert conflict.value.code == "invalid_scope"


@pytest.mark.asyncio
async def test_saved_scope_is_narrowed_and_inactive_document_is_no_match(scope_db):
    async with scope_db() as session:
        active = Document(
            title="Active", source_type=DocumentSourceType.note, is_deleted=False
        )
        deleted = Document(
            title="Deleted", source_type=DocumentSourceType.note, is_deleted=True
        )
        session.add_all([active, deleted])
        await session.flush()
        scope = KnowledgeScope(
            name="Selected notes",
            slug="selected-notes",
            filter={
                "source_types": ["note"],
                "document_ids": [active.id, deleted.id],
            },
        )
        session.add(scope)
        await session.commit()

        resolver = KnowledgeScopeResolver(session)
        resolved = await resolver.resolve(
            scope_slug="selected-notes", document_ids=[active.id]
        )
        assert resolved.scope_id == scope.id
        assert resolved.document_ids == [active.id]
        assert resolved.matches_none is False

        empty = await resolver.resolve(
            scope_slug="selected-notes", document_ids=[deleted.id]
        )
        assert empty.document_ids == []
        assert empty.matches_none is True


async def _add_connector(session, documents: list[Document]) -> WebDAVSource:
    source = WebDAVSource(
        name="Test Drive",
        base_url="https://example.com/dav/",
        username="user",
        password_cipher="encrypted",
        root_path="/",
        include_extensions=[],
        ignore_patterns=[],
    )
    session.add(source)
    await session.flush()
    for index, document in enumerate(documents):
        session.add(
            WebDAVEntry(
                source_id=source.id,
                remote_path=f"/doc-{index}.md",
                document_id=document.id,
            )
        )
    await session.flush()
    return source


@pytest.mark.asyncio
async def test_connector_scope_only_returns_active_linked_documents(scope_db):
    async with scope_db() as session:
        active = Document(
            title="Active", source_type=DocumentSourceType.file, is_deleted=False
        )
        deleted = Document(
            title="Deleted", source_type=DocumentSourceType.file, is_deleted=True
        )
        other = Document(
            title="Other", source_type=DocumentSourceType.file, is_deleted=False
        )
        session.add_all([active, deleted, other])
        await session.flush()
        source = await _add_connector(session, [active, deleted])
        await session.commit()

        resolver = KnowledgeScopeResolver(session)
        resolved = await resolver.resolve(connector_ids=[source.id])
        assert resolved.document_ids == [active.id]

        empty = await resolver.resolve(
            connector_ids=[source.id], document_ids=[other.id]
        )
        assert empty.document_ids == []
        assert empty.matches_none is True


@pytest.mark.asyncio
async def test_connector_without_documents_is_no_match(scope_db):
    async with scope_db() as session:
        source = await _add_connector(session, [])
        await session.commit()
        resolved = await KnowledgeScopeResolver(session).resolve(
            connector_ids=[source.id]
        )
        assert resolved.matches_none is True


@pytest.mark.asyncio
async def test_model_serialization(scope_db):
    async with scope_db() as session:
        scope = KnowledgeScope(
            name="Favorite papers",
            slug="favorite-papers",
            description="Curated",
            filter={"category_ids": [1, 3]},
        )
        session.add(scope)
        await session.commit()
        data = scope.to_public_dict()
        assert data["slug"] == "favorite-papers"
        assert data["filter"] == {"category_ids": [1, 3]}
        assert data["version"] == 1
        assert data["created_at"]
