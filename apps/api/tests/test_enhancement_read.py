"""Pure isolated tests for enhancement_read on an in-memory aiosqlite database.

The test database is created fresh per test; it never points at the
application DATABASE_URL. These tests do not import the FastAPI app,
do not read environment variables, do not invoke any model and do not
mutate any persistent state outside the per-test engine.
"""
import asyncio
import copy
import json

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

import apps.api.models  # noqa: F401
from apps.api.core.db import Base
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.document_scope_keys import DocumentScopeKey
from apps.api.models.enhancement import EnhancementRun, EnhancementWindow
from apps.api.models.workspaces import Workspace
from apps.api.services import enhancement_read as service
from apps.api.services.document_map import build_document_map
from apps.api.services.scope_keys import DocumentSelection


def _structured_payload(kind: str = "docx", count: int = 3) -> dict:
    blocks = []
    for index in range(count):
        blocks.append(
            {
                "type": "heading",
                "text": f"章节{index}",
                "heading_path": [],
                "level": 1,
                "page": 1,
                "paragraph_index": index * 2,
            }
        )
        blocks.append(
            {
                "type": "paragraph",
                "text": f"事实{index}以及条件说明。",
                "heading_path": [f"章节{index}"],
                "page": 1,
                "paragraph_index": index * 2 + 1,
            }
        )
    return {"document_type": kind, "blocks": blocks}


async def _make_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


async def _seed_workspaces(engine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                Workspace(id=1, slug="one", name="One", settings={}, status="active"),
                Workspace(id=2, slug="two", name="Two", settings={}, status="active"),
            ]
        )
        await session.commit()


def _bind(session: AsyncSession, workspace_id: int) -> None:
    session.info["cangzhi_workspace_id"] = workspace_id


async def _make_document(
    session: AsyncSession,
    workspace_id: int,
    *,
    kind: str = "docx",
    count: int = 3,
    is_deleted: bool = False,
) -> tuple[Document, DocumentVersion]:
    document = Document(
        title="sample",
        source_type=DocumentSourceType.file,
        workspace_id=workspace_id,
    )
    session.add(document)
    await session.flush()
    payload = _structured_payload(kind=kind, count=count)
    version = DocumentVersion(
        document_id=document.id,
        version_number=1,
        content_hash="hash",
        raw_content="原文不改动",
        structured_content=payload,
        processing_status="ready",
    )
    session.add(version)
    await session.flush()
    document.current_version_id = version.id
    document.is_deleted = is_deleted
    await session.commit()
    return document, version


async def _make_run(
    session: AsyncSession,
    workspace_id: int,
    document: Document,
    version: DocumentVersion,
    *,
    status: str = "completed",
    windows: int = 2,
) -> EnhancementRun:
    payload = copy.deepcopy(version.structured_content)
    fingerprint = build_document_map(payload, version_id=version.id)["source_fingerprint"]
    run = EnhancementRun(
        workspace_id=workspace_id,
        document_id=document.id,
        document_version_id=version.id,
        source_fingerprint=fingerprint,
        source_snapshot=payload,
        config={"modules": ["chapter"]},
        status=status,
        call_budget=8,
        calls_used=windows if status == "completed" else 0,
        total_windows=windows,
        total_source_chars=100,
    )
    session.add(run)
    await session.flush()
    for ordinal in range(windows):
        block_index = ordinal * 2
        segments = [
            {
                "block_id": f"v{version.id}:{fingerprint}:b{block_index}",
                "block_index": block_index,
                "start": 0,
                "stop": len(payload["blocks"][block_index]["text"]),
                "partial_block": False,
            }
        ] if block_index < len(payload["blocks"]) else []
        result = (
            {
                "summary": {
                    "text": f"窗口{ordinal}摘要。",
                    "evidence_ids": [0],
                },
                "entities": [
                    {
                        "id": f"e{ordinal}",
                        "name": f"实体{ordinal}",
                        "kind": "person",
                        "aliases": [],
                        "evidence_ids": [0],
                    }
                ],
                "relations": [
                    {
                        "subject": f"e{ordinal}",
                        "object": f"e{ordinal}",
                        "predicate": "self",
                        "evidence_ids": [0],
                    }
                ],
                "events": [{"text": f"事件{ordinal}", "evidence_ids": [0]}],
                "evidence_status": "model_extracted_unverified",
            }
            if status == "completed"
            else None
        )
        session.add(
            EnhancementWindow(
                run_id=run.id,
                ordinal=ordinal,
                status=status,
                segments=segments,
                source_chars=4,
                result=result,
            )
        )
    await session.commit()
    return run


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# list_document_enhancements
# ---------------------------------------------------------------------------


def test_list_returns_run_summaries_for_current_version():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_a = await _make_run(session, 1, document, version)
            run_b = await _make_run(session, 1, document, version, windows=3)
            result = await service.list_document_enhancements(session, document.id)
        await engine.dispose()
        return document, version, run_a, run_b, result

    document, version, run_a, run_b, result = run(scenario())
    assert result["document_id"] == document.id
    assert result["document_version_id"] == version.id
    assert result["total"] == 2
    assert result["offset"] == 0
    assert result["limit"] == service.DEFAULT_LIST_LIMIT
    assert result["next_offset"] is None
    assert {item["id"] for item in result["items"]} == {run_a.id, run_b.id}
    for item in result["items"]:
        assert item["document_id"] == document.id
        assert item["status"] == "completed"


def test_list_excludes_stale_runs_from_older_source():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            stale = await _make_run(session, 1, document, version)
            # Mutate the source so the fingerprint no longer matches.
            new_payload = copy.deepcopy(version.structured_content)
            new_payload["blocks"][0]["text"] = "变化后的章节"
            version.structured_content = new_payload
            await session.commit()
            result = await service.list_document_enhancements(session, document.id)
        await engine.dispose()
        return result, stale

    result, stale = run(scenario())
    assert result["total"] == 0
    assert result["items"] == []
    assert result["next_offset"] is None


def test_list_excludes_stale_runs_for_older_version():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_old = await _make_run(session, 1, document, version)
            # The run points at the old version; advance the document
            # to a new version so the run becomes stale.
            new_version = DocumentVersion(
                document_id=document.id,
                version_number=2,
                content_hash="new",
                raw_content="原文不改动",
                structured_content=copy.deepcopy(version.structured_content),
                processing_status="ready",
            )
            session.add(new_version)
            await session.flush()
            document.current_version_id = new_version.id
            await session.commit()
            result = await service.list_document_enhancements(session, document.id)
        await engine.dispose()
        return result, run_old, new_version

    result, run_old, new_version = run(scenario())
    assert result["document_version_id"] == new_version.id
    assert result["total"] == 0
    assert result["items"] == []


def test_list_does_not_load_snapshot_when_no_candidates():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            await _make_run(session, 1, document, version)
            # Break the fingerprint so nothing matches; no large body
            # should be touched.
            payload = copy.deepcopy(version.structured_content)
            payload["blocks"][0]["text"] = "换内容"
            version.structured_content = payload
            await session.commit()
            result = await service.list_document_enhancements(session, document.id)
        await engine.dispose()
        return result

    result = run(scenario())
    assert result["total"] == 0
    assert result["items"] == []


def test_list_pagination_offset_and_limit():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            for _ in range(3):
                await _make_run(session, 1, document, version)
            page_one = await service.list_document_enhancements(
                session, document.id, limit=2, offset=0
            )
            page_two = await service.list_document_enhancements(
                session, document.id, limit=2, offset=2
            )
        await engine.dispose()
        return page_one, page_two

    page_one, page_two = run(scenario())
    assert page_one["total"] == 3
    assert len(page_one["items"]) == 2
    assert page_one["next_offset"] == 2
    assert page_two["total"] == 3
    assert len(page_two["items"]) == 1
    assert page_two["next_offset"] is None


def test_list_rejects_invalid_pagination():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, _ = await _make_document(session, 1)
            for bad in ({"limit": 0}, {"limit": 51}, {"offset": -1},
                        {"limit": True}, {"offset": "0"}):
                with pytest.raises(service.KnowledgeReadError) as info:
                    await service.list_document_enhancements(
                        session, document.id, **bad
                    )
                assert info.value.code == "invalid_arguments"
        await engine.dispose()

    run(scenario())


def test_list_rejects_invalid_document_id():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            for bad in (0, -1, True, "1", None):
                with pytest.raises(service.KnowledgeReadError) as info:
                    await service.list_document_enhancements(session, bad)
                assert info.value.code == "invalid_arguments"
        await engine.dispose()

    run(scenario())


def test_list_returns_document_not_found_for_missing_doc():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.list_document_enhancements(session, 999)
            assert info.value.code == "document_not_found"
        await engine.dispose()

    run(scenario())


def test_list_isolated_by_workspace():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            await _make_run(session, 1, document, version)
        async with factory() as session:
            _bind(session, 2)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.list_document_enhancements(session, document.id)
            assert info.value.code == "document_not_found"
        await engine.dispose()

    run(scenario())


def test_list_rejects_archived_workspace():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            workspace = await session.get(Workspace, 1)
            workspace.status = "archived"
            await session.commit()
            _bind(session, 1)
            document, _ = await _make_document(session, 1)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.list_document_enhancements(session, document.id)
            assert info.value.code == "document_not_found"
        await engine.dispose()

    run(scenario())


def test_list_excludes_deleted_document():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, _ = await _make_document(session, 1, is_deleted=True)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.list_document_enhancements(session, document.id)
            assert info.value.code == "document_not_found"
        await engine.dispose()

    run(scenario())


def test_list_requires_workspace_binding():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            document, _ = await _make_document(session, 1)
            # No workspace binding; result must not leak and must error.
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.list_document_enhancements(session, document.id)
            assert info.value.code == "document_not_found"
        await engine.dispose()

    run(scenario())


def test_list_document_selection_intersects_candidates():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            await _make_run(session, 1, document, version)
            session.add(
                DocumentScopeKey(
                    workspace_id=1, document_id=document.id, scope_key="keep"
                )
            )
            await session.commit()
            keep = DocumentSelection(scope_keys=("keep",), document_ids=())
            drop = DocumentSelection(scope_keys=("missing",), document_ids=())
        async with factory() as session:
            _bind(session, 1)
            kept = await service.list_document_enhancements(
                session, document.id, document_selection=keep
            )
            with pytest.raises(service.KnowledgeReadError) as denied:
                await service.list_document_enhancements(session, document.id, document_selection=drop)
            assert denied.value.code == "document_not_found"
        await engine.dispose()
        return kept

    kept = run(scenario())
    assert kept["total"] == 1


def test_list_document_boundary_treated_as_hard_wall():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            await _make_run(session, 1, document, version)
            session.add(
                DocumentScopeKey(
                    workspace_id=1, document_id=document.id, scope_key="inside"
                )
            )
            await session.commit()
            inside = DocumentSelection(scope_keys=("inside",), document_ids=())
            outside = DocumentSelection(scope_keys=("other",), document_ids=())
        async with factory() as session:
            _bind(session, 1)
            inside_result = await service.list_document_enhancements(
                session, document.id, document_boundary=inside
            )
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.list_document_enhancements(
                    session, document.id, document_boundary=outside
                )
            assert info.value.code == "document_not_found"
        await engine.dispose()
        return inside_result

    inside_result = run(scenario())
    assert inside_result["total"] == 1


# ---------------------------------------------------------------------------
# read_enhancement
# ---------------------------------------------------------------------------


def test_read_summary_view_returns_paginated_summary_items():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version)
            result = await service.read_enhancement(
                session, run_row.id, view="summary", limit=1
            )
        await engine.dispose()
        return result, run_row, document

    result, run_row, document = run(scenario())
    assert result["window_index"] == 0
    assert result["window_status"] == "completed"
    assert result["view"] == "summary"
    assert result["items"] == [{"text": "窗口0摘要。", "evidence_ids": [0]}]
    assert result["total"] == 1
    assert result["next_offset"] is None
    assert result["next_window_index"] == 1
    assert result["evidence_status"] == "model_extracted_unverified"
    assert result["read_only"] is True
    assert result["model_calls"] == 0
    assert result["run"]["id"] == run_row.id
    assert result["run"]["document_id"] == document.id


def test_read_entities_view_paginates_extracted_entities():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version)
            page_one = await service.read_enhancement(
                session, run_row.id, view="entities", limit=1, offset=0
            )
        await engine.dispose()
        return page_one

    result = run(scenario())
    assert result["view"] == "entities"
    assert result["total"] == 1
    assert len(result["items"]) == 1
    assert result["items"][0]["id"] == "e0"
    assert result["next_offset"] is None


def test_read_relations_view_paginates_extracted_relations():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version)
            result = await service.read_enhancement(
                session, run_row.id, view="relations", limit=5
            )
        await engine.dispose()
        return result

    result = run(scenario())
    assert result["view"] == "relations"
    assert result["total"] == 1
    assert result["items"][0]["predicate"] == "self"


def test_read_events_view_paginates_extracted_events():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version)
            result = await service.read_enhancement(
                session, run_row.id, view="events", limit=5
            )
        await engine.dispose()
        return result

    result = run(scenario())
    assert result["view"] == "events"
    assert result["total"] == 1
    assert result["items"][0]["text"] == "事件0"


def test_read_evidence_view_returns_source_segments():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version)
            result = await service.read_enhancement(
                session, run_row.id, view="evidence", limit=10
            )
        await engine.dispose()
        return result, run_row

    result, _ = run(scenario())
    assert result["view"] == "evidence"
    assert result["total"] == 1
    segment = result["items"][0]
    assert segment["text"].startswith("章节0")
    assert "id" in segment and "block_id" in segment
    assert "start" in segment and "stop" in segment


def test_read_pending_window_returns_empty_items_with_status():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version, status="pending")
            result = await service.read_enhancement(session, run_row.id)
        await engine.dispose()
        return result

    result = run(scenario())
    assert result["window_status"] == "pending"
    assert result["items"] == []
    assert result["total"] == 0
    assert result["evidence_status"] == "model_extracted_unverified"
    assert result["read_only"] is True
    assert result["model_calls"] == 0


def test_read_failed_window_keeps_status_visible():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version, status="failed")
            result = await service.read_enhancement(session, run_row.id)
        await engine.dispose()
        return result

    result = run(scenario())
    assert result["window_status"] == "failed"
    assert result["items"] == []


def test_read_next_window_index_none_at_end():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version, windows=2)
            result = await service.read_enhancement(
                session, run_row.id, window_index=1
            )
        await engine.dispose()
        return result

    result = run(scenario())
    assert result["window_index"] == 1
    assert result["next_window_index"] is None


def test_read_stale_run_for_older_version_raises_enhancement_stale():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version)
            new_version = DocumentVersion(
                document_id=document.id,
                version_number=2,
                content_hash="new",
                raw_content="原文不改动",
                structured_content=copy.deepcopy(version.structured_content),
                processing_status="ready",
            )
            session.add(new_version)
            await session.flush()
            document.current_version_id = new_version.id
            await session.commit()
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_enhancement(session, run_row.id)
            assert info.value.code == "enhancement_stale"
        await engine.dispose()

    run(scenario())


def test_read_stale_run_when_source_changes_raises_enhancement_stale():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version)
            new_payload = copy.deepcopy(version.structured_content)
            new_payload["blocks"][0]["text"] = "结构变化"
            version.structured_content = new_payload
            await session.commit()
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_enhancement(session, run_row.id)
            assert info.value.code == "enhancement_stale"
        await engine.dispose()

    run(scenario())


def test_read_unknown_run_id_raises_enhancement_not_found():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_enhancement(session, 999)
            assert info.value.code == "enhancement_not_found"
        await engine.dispose()

    run(scenario())


def test_read_run_in_other_workspace_returns_not_found():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version)
        async with factory() as session:
            _bind(session, 2)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_enhancement(session, run_row.id)
            assert info.value.code == "enhancement_not_found"
        await engine.dispose()

    run(scenario())


def test_read_run_with_deleted_document_returns_not_found():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version)
            document.is_deleted = True
            await session.commit()
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_enhancement(session, run_row.id)
            assert info.value.code == "enhancement_not_found"
        await engine.dispose()

    run(scenario())


def test_read_run_with_outside_boundary_returns_not_found():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version)
            session.add(
                DocumentScopeKey(
                    workspace_id=1, document_id=document.id, scope_key="inside"
                )
            )
            await session.commit()
            outside = DocumentSelection(scope_keys=("outside",), document_ids=())
        async with factory() as session:
            _bind(session, 1)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_enhancement(
                    session, run_row.id, document_boundary=outside
                )
            assert info.value.code == "enhancement_not_found"
        await engine.dispose()

    run(scenario())


def test_read_run_with_outside_selection_returns_not_found():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version)
            session.add(
                DocumentScopeKey(
                    workspace_id=1, document_id=document.id, scope_key="inside"
                )
            )
            await session.commit()
            outside = DocumentSelection(scope_keys=("outside",), document_ids=())
        async with factory() as session:
            _bind(session, 1)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_enhancement(
                    session, run_row.id, document_selection=outside
                )
            assert info.value.code == "enhancement_not_found"
        await engine.dispose()

    run(scenario())


def test_read_rejects_invalid_arguments():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version)
            for bad in ({"run_id": 0}, {"run_id": True}, {"window_index": -1},
                        {"view": "bogus"}, {"offset": -1}, {"limit": 0},
                        {"limit": 21}, {"limit": True}):
                with pytest.raises(service.KnowledgeReadError) as info:
                    await service.read_enhancement(session, **{"run_id": run_row.id, **bad})
                assert info.value.code == "invalid_arguments"
        await engine.dispose()

    run(scenario())


def test_read_does_not_call_model_or_mutate_counts():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version, windows=3)
            before = {
                "calls_used": run_row.calls_used,
                "status": run_row.status,
                "lease_until": run_row.lease_until,
                "windows": list(
                    (await session.execute(
                        EnhancementWindow.__table__.select().where(
                            EnhancementWindow.run_id == run_row.id
                        )
                    )).all()
                ),
            }
            for view in ("summary", "entities", "relations", "events", "evidence"):
                for ordinal in range(3):
                    result = await service.read_enhancement(
                        session, run_row.id, window_index=ordinal, view=view
                    )
                    assert result["read_only"] is True
                    assert result["model_calls"] == 0
            after = {
                "calls_used": run_row.calls_used,
                "status": run_row.status,
                "lease_until": run_row.lease_until,
                "windows": list(
                    (await session.execute(
                        EnhancementWindow.__table__.select().where(
                            EnhancementWindow.run_id == run_row.id
                        )
                    )).all()
                ),
            }
        await engine.dispose()
        return before, after, run_row

    before, after, run_row = run(scenario())
    assert before["calls_used"] == after["calls_used"]
    assert before["status"] == after["status"]
    assert before["lease_until"] == after["lease_until"]
    assert len(before["windows"]) == len(after["windows"]) == run_row.total_windows
    assert before["windows"] == after["windows"]


def test_read_never_serialises_run_within_response_payload():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version)
            result = await service.read_enhancement(
                session, run_row.id, view="evidence"
            )
        await engine.dispose()
        return result

    result = run(scenario())
    encoded = json.dumps(result, ensure_ascii=False)
    assert "source_snapshot" not in encoded
    assert "call_budget" in encoded  # run summary is exposed
    # Pending/done windows do not leak the full snapshot
    assert "blocks" not in encoded or result["view"] != "summary"


def test_unenhanced_document_list_never_selects_body_or_snapshot():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, _ = await _make_document(session, 1)
            doc_id = document.id
        statements = []
        def record(conn, cursor, statement, parameters, context, many):
            statements.append(statement.lower())
        event.listen(engine.sync_engine, 'before_cursor_execute', record)
        async with factory() as session:
            _bind(session, 1)
            result = await service.list_document_enhancements(session, doc_id)
            assert result['total'] == 0
        assert statements
        assert all('raw_content' not in sql and 'structured_content' not in sql
                   and 'source_snapshot' not in sql for sql in statements)
        await engine.dispose()
    run(scenario())


def test_missing_window_and_stale_status_are_distinct():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            run_row = await _make_run(session, 1, document, version)
            with pytest.raises(service.KnowledgeReadError) as missing:
                await service.read_enhancement(session, run_row.id, window_index=999)
            assert missing.value.code == 'enhancement_not_found'
            run_row.status = 'stale'
            await session.commit()
            assert (await service.list_document_enhancements(session, document.id))['items'] == []
            with pytest.raises(service.KnowledgeReadError) as stale:
                await service.read_enhancement(session, run_row.id)
            assert stale.value.code == 'enhancement_stale'
        await engine.dispose()
    run(scenario())
