"""Pure isolated tests for source_navigation on an in-memory aiosqlite database.

The test database is created fresh per test; it never points at the
application DATABASE_URL. These tests do not import the FastAPI app,
do not read environment variables, do not invoke any model and do not
mutate any persistent state outside the per-test engine. They also
verify that the length pre-check never materialises the JSON body.
"""
import asyncio
import copy
import json

import pytest
from sqlalchemy import event
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
from apps.api.models.workspaces import Workspace
from apps.api.services import source_navigation as service
from apps.api.services.document_map import build_document_map
from apps.api.services.scope_keys import DocumentSelection


def _heading(text: str, *, page: int = 1, paragraph_index: int = 0) -> dict:
    return {
        "type": "heading",
        "text": text,
        "heading_path": [],
        "level": 1,
        "page": page,
        "paragraph_index": paragraph_index,
    }


def _paragraph(text: str, *, page: int = 1, paragraph_index: int = 0) -> dict:
    return {
        "type": "paragraph",
        "text": text,
        "heading_path": [],
        "page": page,
        "paragraph_index": paragraph_index,
    }


def _table(text: str, *, page: int = 1) -> dict:
    return {
        "type": "table",
        "text": text,
        "heading_path": [],
        "page": page,
    }


def _code(text: str, *, page: int = 1) -> dict:
    return {
        "type": "code_block",
        "text": text,
        "heading_path": [],
        "page": page,
    }


def _list(text: str, *, page: int = 1) -> dict:
    return {
        "type": "list_item",
        "text": text,
        "heading_path": [],
        "page": page,
    }


def _fixture_payload() -> dict:
    return {
        "document_type": "docx",
        "blocks": [
            _heading("第一章 总则"),
            _paragraph("本标准规定了车辆分类与质量门槛。"),
            _table("类别|重量\nM1|≤3500kg\nM2|≤5000kg\nM3|>5000kg"),
            _code("def hello():\n    return 1"),
            _list("适用条件一"),
            _list("适用条件二"),
            _paragraph(""),
            _paragraph("重复内容用于验证 identity。重复内容用于验证 identity。"),
            _paragraph("长度扩展用文本。" * 60),
        ],
    }


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
    payload: dict | None = None,
    is_deleted: bool = False,
    document_type: str | None = None,
) -> tuple[Document, DocumentVersion]:
    document = Document(
        title="sample",
        source_type=DocumentSourceType.file,
        workspace_id=workspace_id,
    )
    session.add(document)
    await session.flush()
    body = payload or _fixture_payload()
    if document_type is not None:
        body = {**body, "document_type": document_type}
    version = DocumentVersion(
        document_id=document.id,
        version_number=1,
        content_hash="hash",
        raw_content="原文不改动",
        structured_content=body,
        processing_status="ready",
    )
    session.add(version)
    await session.flush()
    document.current_version_id = version.id
    document.is_deleted = is_deleted
    await session.commit()
    return document, version


def run(coro):
    return asyncio.run(coro)


def test_bounded_paths_missing_blocks_and_single_map_build(monkeypatch):
    original = service.build_document_map
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(service, 'build_document_map', counted)
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                _bind(session, 1)
                payload = {'document_type': 'docx', 'blocks': [
                    {'type': 'paragraph', 'text': '真实原文', 'heading_path': ['长' * 500] * 30}]}
                doc, version = await _make_document(session, 1, payload=payload)
                mapped = await service.read_document_map(session, doc.id, view='blocks')
                assert len(calls) == 1
                item = mapped['items'][0]
                assert item['heading_path_truncated']
                assert len(item['heading_path']) == 16 and len(item['heading_path'][0]) == 200
                block = await service.read_document_block(session, doc.id, block_id=item['id'])
                assert len(calls) == 2  # Not twice per block read.
                assert block['block']['text'] == '真实原文'
                assert not session.dirty
                version.structured_content = {'document_type': 'docx'}
                await session.commit()
                with pytest.raises(service.KnowledgeReadError) as error:
                    await service.read_document_map(session, doc.id)
                assert error.value.code == 'structure_unavailable'
        finally:
            await engine.dispose()
    run(scenario())


def test_body_query_enforces_limit_after_precheck(monkeypatch):
    # Simulate a stale precheck result: the subsequent SQL body query must still
    # enforce the size predicate and avoid loading a newly oversized payload.
    async def stale_check(db, version_id):
        return 1
    monkeypatch.setattr(service, '_serialised_length', stale_check)
    monkeypatch.setattr(service, 'MAX_SERIALIZED_CHARS', 80)
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                _bind(session, 1)
                doc, _ = await _make_document(session, 1)
                with pytest.raises(service.KnowledgeReadError) as error:
                    await service.read_document_map(session, doc.id)
                assert error.value.code == 'source_changed'
        finally:
            await engine.dispose()
    run(scenario())


# ---------------------------------------------------------------------------
# read_document_map
# ---------------------------------------------------------------------------


def test_outline_returns_only_headings_with_title_metadata():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            result = await service.read_document_map(session, document.id)
        await engine.dispose()
        return result, document, version

    result, document, version = run(scenario())
    assert result["document_id"] == document.id
    assert result["document_version_id"] == version.id
    assert result["view"] == "outline"
    assert result["total"] == 1
    assert result["offset"] == 0
    assert result["limit"] == 20
    assert result["next_offset"] is None
    assert result["read_only"] is True
    assert result["model_calls"] == 0
    assert result["source_fingerprint"]
    item = result["items"][0]
    assert item["type"] == "heading"
    assert item["title"] == "第一章 总则"
    assert item["title_truncated"] is False
    # Outline items must not include any raw source text from other block types.
    for entry in result["items"]:
        assert "text" not in entry
        assert "char_count" in entry


def test_blocks_view_returns_all_entries_in_source_order():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, _ = await _make_document(session, 1)
            result = await service.read_document_map(
                session, document.id, view="blocks"
            )
        await engine.dispose()
        return result

    result = run(scenario())
    types = [item["type"] for item in result["items"]]
    assert "heading" in types
    assert set(types) <= {"heading", "paragraph", "table", "code_block", "list_item", "blockquote"}
    assert [item["block_index"] for item in result["items"]] == list(range(result["total"]))
    assert result["total"] == len(result["items"])
    for entry in result["items"]:
        assert "text" not in entry
        assert "title" not in entry


def test_outline_title_truncates_to_200_chars():
    long_title = "标题" * 200
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            payload = {"document_type": "docx", "blocks": [_heading(long_title)]}
            document, _ = await _make_document(session, 1, payload=payload)
            result = await service.read_document_map(session, document.id)
        await engine.dispose()
        return result

    result = run(scenario())
    item = result["items"][0]
    assert item["title"] == "标题" * 100  # 200 / 2 chars per "标题"
    assert item["title_truncated"] is True


def test_blocks_view_paginates_offset_and_limit():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, _ = await _make_document(session, 1)
            page_one = await service.read_document_map(
                session, document.id, view="blocks", limit=2, offset=0
            )
            page_two = await service.read_document_map(
                session, document.id, view="blocks", limit=2, offset=2
            )
        await engine.dispose()
        return page_one, page_two

    page_one, page_two = run(scenario())
    assert page_one["total"] == page_two["total"]
    assert page_one["total"] >= 4
    assert len(page_one["items"]) == 2
    assert page_one["next_offset"] == 2
    assert page_two["next_offset"] in (None, 4)


def test_outline_rejects_invalid_pagination():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, _ = await _make_document(session, 1)
            for bad in ({"limit": 0}, {"limit": 101}, {"offset": -1},
                        {"limit": True}, {"offset": "0"}):
                with pytest.raises(service.KnowledgeReadError) as info:
                    await service.read_document_map(session, document.id, **bad)
                assert info.value.code == "invalid_arguments"
        await engine.dispose()

    run(scenario())


def test_outline_rejects_invalid_view_and_document_id():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            for bad in (0, -1, True, "1", None):
                with pytest.raises(service.KnowledgeReadError) as info:
                    await service.read_document_map(session, bad)
                assert info.value.code == "invalid_arguments"
            document, _ = await _make_document(session, 1)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_map(session, document.id, view="bogus")
            assert info.value.code == "invalid_arguments"
        await engine.dispose()

    run(scenario())


def test_map_returns_not_found_for_missing_document():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_map(session, 999)
            assert info.value.code == "document_not_found"
        await engine.dispose()

    run(scenario())


def test_map_isolated_by_workspace():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, _ = await _make_document(session, 1)
        async with factory() as session:
            _bind(session, 2)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_map(session, document.id)
            assert info.value.code == "document_not_found"
        await engine.dispose()

    run(scenario())


def test_map_rejects_archived_workspace():
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
                await service.read_document_map(session, document.id)
            assert info.value.code == "document_not_found"
        await engine.dispose()

    run(scenario())


def test_map_excludes_deleted_document():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, _ = await _make_document(session, 1, is_deleted=True)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_map(session, document.id)
            assert info.value.code == "document_not_found"
        await engine.dispose()

    run(scenario())


def test_map_document_selection_intersects_candidates():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, _ = await _make_document(session, 1)
            keep_doc, _ = await _make_document(session, 1)
            session.add_all(
                [
                    DocumentScopeKey(workspace_id=1, document_id=document.id,
                                     scope_key="keep"),
                    DocumentScopeKey(workspace_id=1, document_id=keep_doc.id,
                                     scope_key="keep"),
                ]
            )
            await session.commit()
            keep = DocumentSelection(scope_keys=("keep",), document_ids=())
            drop = DocumentSelection(scope_keys=("missing",), document_ids=())
            explicit = DocumentSelection(scope_keys=(), document_ids=(document.id,))
            union_only_doc = DocumentSelection(
                scope_keys=("missing",), document_ids=(document.id,)
            )
        async with factory() as session:
            _bind(session, 1)
            kept = await service.read_document_map(
                session, document.id, document_selection=keep
            )
            explicit_hit = await service.read_document_map(
                session, document.id, document_selection=explicit
            )
            union_hit = await service.read_document_map(
                session, document.id, document_selection=union_only_doc
            )
            with pytest.raises(service.KnowledgeReadError) as denied:
                await service.read_document_map(
                    session, document.id, document_selection=drop
                )
            assert denied.value.code == "document_not_found"
        await engine.dispose()
        return kept, explicit_hit, union_hit

    kept, explicit_hit, union_hit = run(scenario())
    assert kept["total"] == 1
    assert explicit_hit["total"] == 1
    assert union_hit["total"] == 1


def test_map_document_boundary_treated_as_hard_wall():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, _ = await _make_document(session, 1)
            session.add(DocumentScopeKey(workspace_id=1, document_id=document.id,
                                          scope_key="inside"))
            await session.commit()
            inside = DocumentSelection(scope_keys=("inside",), document_ids=())
            outside = DocumentSelection(scope_keys=("other",), document_ids=())
        async with factory() as session:
            _bind(session, 1)
            inside_result = await service.read_document_map(
                session, document.id, document_boundary=inside
            )
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_map(
                    session, document.id, document_boundary=outside
                )
            assert info.value.code == "document_not_found"
        await engine.dispose()
        return inside_result

    inside_result = run(scenario())
    assert inside_result["total"] == 1


def test_map_dataset_document_returns_structure_unavailable():
    for kind in ("xls", "xlsx", "database_table"):
        async def scenario(kind=kind):
            engine = await _make_engine()
            await _seed_workspaces(engine)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                _bind(session, 1)
                document, _ = await _make_document(session, 1, document_type=kind)
                with pytest.raises(service.KnowledgeReadError) as info:
                    await service.read_document_map(session, document.id)
                assert info.value.code == "structure_unavailable"
                assert "数据集" in str(info.value)
            await engine.dispose()

        run(scenario())


def test_map_missing_structure_returns_structure_unavailable():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document = Document(
                title="empty",
                source_type=DocumentSourceType.file,
                workspace_id=1,
            )
            session.add(document)
            await session.flush()
            version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                content_hash="hash",
                raw_content="",
                structured_content=None,
                processing_status="ready",
            )
            session.add(version)
            await session.flush()
            document.current_version_id = version.id
            await session.commit()
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_map(session, document.id)
            assert info.value.code == "structure_unavailable"
        await engine.dispose()

    run(scenario())


@pytest.mark.parametrize("invalid_type", ["invented", [], {}])
def test_map_malformed_structure_returns_structure_unavailable(invalid_type):
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            payload = {"document_type": "docx", "blocks": [
                {"type": invalid_type, "text": "未知类型", "heading_path": []}
            ]}
            document, _ = await _make_document(session, 1, payload=payload)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_map(session, document.id)
            assert info.value.code == "structure_unavailable"
        await engine.dispose()

    run(scenario())


# ---------------------------------------------------------------------------
# read_document_block
# ---------------------------------------------------------------------------


def test_block_returns_exact_source_text_with_metadata():
    engine = asyncio.run(_make_engine())
    asyncio.run(_seed_workspaces(engine))
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def setup():
            async with factory() as session:
                _bind(session, 1)
                document, version = await _make_document(session, 1)
                payload = version.structured_content
                fingerprint = build_document_map(payload, version_id=version.id)[
                    "source_fingerprint"
                ]
                blocks = build_document_map(payload, version_id=version.id)["blocks"]

                def by_type(t, idx=0):
                    matches = [b for b in blocks if b["type"] == t]
                    return matches[idx]["id"]

                table_anchor = by_type("table")
                code_anchor = by_type("code_block")
                list_anchor = by_type("list_item")
                empty_anchor = next(
                    b["id"] for b in blocks
                    if b["type"] == "paragraph" and b["block_index"] == 6
                )
                long_anchor = next(
                    b["id"] for b in blocks
                    if b["type"] == "paragraph" and b["char_count"] > 100
                )
                repeated_anchor = next(
                    b["id"] for b in blocks
                    if b["type"] == "paragraph" and b["block_index"] == 7
                )
                results = {}
                for name, anchor, kwargs in [
                    ("table", table_anchor, {}),
                    ("code", code_anchor, {}),
                    ("list", list_anchor, {}),
                    ("empty", empty_anchor, {}),
                    ("long_paged", long_anchor, {"max_chars": 10, "offset": 0}),
                    ("repeated", repeated_anchor, {}),
                    ("long_full", long_anchor, {"max_chars": 12000}),
                ]:
                    async with factory() as session:
                        _bind(session, 1)
                        results[name] = await service.read_document_block(
                            session, document.id, block_id=anchor, **kwargs
                        )
                return {
                    "results": results,
                    "payload": payload,
                    "fingerprint": fingerprint,
                    "document_id": document.id,
                    "version_id": version.id,
                }

        data = asyncio.run(setup())
    finally:
        asyncio.run(engine.dispose())

    results = data["results"]
    table = results["table"]
    assert table["document_id"] == data["document_id"]
    assert table["document_version_id"] == data["version_id"]
    assert table["source_fingerprint"] == data["fingerprint"]
    assert table["read_only"] is True
    assert table["model_calls"] == 0
    assert table["block"]["text"] == "类别|重量\nM1|≤3500kg\nM2|≤5000kg\nM3|>5000kg"
    assert table["block"]["type"] == "table"
    assert table["block"]["partial"] is False
    assert results["code"]["block"]["text"] == "def hello():\n    return 1"
    assert results["list"]["block"]["text"] == "适用条件一"
    assert results["empty"]["block"]["text"] == ""
    expected_repeat = "重复内容用于验证 identity。重复内容用于验证 identity。"
    assert results["repeated"]["block"]["text"] == expected_repeat
    paged = results["long_paged"]
    assert paged["block"]["partial"] is True
    assert paged["block"]["next_offset"] == 10
    full = results["long_full"]
    assert full["block"]["next_offset"] is None
    assert len(full["block"]["text"]) == full["block"]["char_count"]


def test_block_rejects_invalid_arguments():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, _ = await _make_document(session, 1)
            for bad in ({"block_id": ""}, {"block_id": "x" * 161},
                        {"block_id": 1}, {"offset": -1}, {"offset": 2_000_001},
                        {"max_chars": 0}, {"max_chars": 12001},
                        {"max_chars": True}, {"offset": "0"}):
                with pytest.raises(service.KnowledgeReadError) as info:
                    await service.read_document_block(session, document.id, **{"block_id": "x", **bad})
                assert info.value.code == "invalid_arguments"
        await engine.dispose()

    run(scenario())


def test_block_offset_out_of_range_rejected():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            payload = version.structured_content
            anchor = build_document_map(payload, version_id=version.id)["blocks"][0]["id"]
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_block(
                    session, document.id, block_id=anchor, offset=10_000
                )
            assert info.value.code == "invalid_arguments"
        await engine.dispose()

    run(scenario())


def test_block_forged_id_raises_source_changed():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            payload = version.structured_content
            forged = f"v{version.id}:deadbeef:b0"
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_block(session, document.id, block_id=forged)
            assert info.value.code == "source_changed"
        await engine.dispose()

    run(scenario())


def test_block_stale_after_source_change_raises_source_changed():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            payload = version.structured_content
            anchor = build_document_map(payload, version_id=version.id)["blocks"][0]["id"]
            # Mutate the source so the fingerprint no longer matches.
            new_payload = copy.deepcopy(payload)
            new_payload["blocks"][0]["text"] = "结构变化"
            version.structured_content = new_payload
            await session.commit()
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_block(session, document.id, block_id=anchor)
            assert info.value.code == "source_changed"
        await engine.dispose()

    run(scenario())


def test_block_stale_after_version_advance_raises_source_changed():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            payload = version.structured_content
            anchor = build_document_map(payload, version_id=version.id)["blocks"][0]["id"]
            new_version = DocumentVersion(
                document_id=document.id,
                version_number=2,
                content_hash="new",
                raw_content="原文不改动",
                structured_content=copy.deepcopy(payload),
                processing_status="ready",
            )
            session.add(new_version)
            await session.flush()
            document.current_version_id = new_version.id
            await session.commit()
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_block(session, document.id, block_id=anchor)
            assert info.value.code == "source_changed"
        await engine.dispose()

    run(scenario())


def test_block_dataset_document_returns_structure_unavailable():
    for kind in ("xls", "xlsx", "database_table"):
        async def scenario(kind=kind):
            engine = await _make_engine()
            await _seed_workspaces(engine)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                _bind(session, 1)
                document, _ = await _make_document(session, 1, document_type=kind)
                with pytest.raises(service.KnowledgeReadError) as info:
                    await service.read_document_block(session, document.id, block_id="x")
                assert info.value.code == "structure_unavailable"
            await engine.dispose()

        run(scenario())


def test_block_respects_workspace_selection_and_boundary():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            session.add(DocumentScopeKey(workspace_id=1, document_id=document.id,
                                          scope_key="inside"))
            await session.commit()
            payload = version.structured_content
            anchor = build_document_map(payload, version_id=version.id)["blocks"][0]["id"]
            inside = DocumentSelection(scope_keys=("inside",), document_ids=())
            outside = DocumentSelection(scope_keys=("other",), document_ids=())
        async with factory() as session:
            _bind(session, 1)
            ok = await service.read_document_block(
                session, document.id, block_id=anchor, document_boundary=inside
            )
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_block(
                    session, document.id, block_id=anchor, document_selection=outside
                )
            assert info.value.code == "document_not_found"
        async with factory() as session:
            _bind(session, 2)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_block(session, document.id, block_id=anchor)
            assert info.value.code == "document_not_found"
        await engine.dispose()
        return ok, anchor

    ok, anchor = run(scenario())
    assert ok["block"]["id"] == anchor
    assert ok["block"]["text"] == "第一章 总则"


def test_block_recycled_document_returns_not_found():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1, is_deleted=True)
            payload = version.structured_content
            anchor = build_document_map(payload, version_id=version.id)["blocks"][0]["id"]
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_block(session, document.id, block_id=anchor)
            assert info.value.code == "document_not_found"
        await engine.dispose()

    run(scenario())


# ---------------------------------------------------------------------------
# Size pre-check (body not loaded)
# ---------------------------------------------------------------------------


def test_length_precheck_rejects_oversize_before_body_load(monkeypatch):
    monkeypatch.setattr(service, "MAX_SERIALIZED_CHARS", 80)

    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        statements: list[str] = []
        def record(conn, cursor, statement, parameters, context, many):
            statements.append(statement.lower())
        event.listen(engine.sync_engine, "before_cursor_execute", record)
        async with factory() as session:
            _bind(session, 1)
            document, _ = await _make_document(session, 1)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_map(session, document.id)
            assert info.value.code == "structure_too_large"
        await engine.dispose()
        return statements

    statements = run(scenario())
    # The length pre-check is the SQL query against structured_content;
    # no SELECT should have pulled the JSON column body anywhere.
    body_selects = [s for s in statements if s.lstrip().startswith("select") and "structured_content" in s and "length" not in s]
    assert body_selects == []


def test_length_precheck_rejects_oversize_block(monkeypatch):
    monkeypatch.setattr(service, "MAX_SERIALIZED_CHARS", 80)

    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, _ = await _make_document(session, 1)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_block(session, document.id, block_id="x")
            assert info.value.code == "structure_too_large"
        await engine.dispose()

    run(scenario())


def test_block_count_limit_rejects_huge_payload(monkeypatch):
    monkeypatch.setattr(service, "MAX_BLOCKS", 2)

    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, _ = await _make_document(session, 1)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_map(
                    session, document.id, view="blocks"
                )
            assert info.value.code == "structure_too_large"
        await engine.dispose()

    run(scenario())


def test_source_char_limit_rejects_huge_payload(monkeypatch):
    monkeypatch.setattr(service, "MAX_SOURCE_CHARS", 20)

    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, _ = await _make_document(session, 1)
            with pytest.raises(service.KnowledgeReadError) as info:
                await service.read_document_block(
                    session, document.id, block_id="x"
                )
            assert info.value.code == "structure_too_large"
        await engine.dispose()

    run(scenario())


# ---------------------------------------------------------------------------
# No mutation / no model call
# ---------------------------------------------------------------------------


def test_map_and_block_do_not_mutate_run_or_version_state():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, version = await _make_document(session, 1)
            payload_before = copy.deepcopy(version.structured_content)
            raw_before = version.raw_content
            await service.read_document_map(
                session, document.id, view="blocks", limit=2, offset=0
            )
            anchor = build_document_map(
                copy.deepcopy(payload_before), version_id=version.id
            )["blocks"][1]["id"]
            await service.read_document_block(
                session, document.id, block_id=anchor
            )
        async with factory() as session:
            _bind(session, 1)
            refreshed = await session.get(DocumentVersion, version.id)
            assert refreshed.structured_content == payload_before
            assert refreshed.raw_content == raw_before
            doc = await session.get(Document, document.id)
            assert doc.is_deleted is False
        await engine.dispose()

    run(scenario())


def test_map_and_block_never_serialise_full_source():
    async def scenario():
        engine = await _make_engine()
        await _seed_workspaces(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            _bind(session, 1)
            document, _ = await _make_document(session, 1)
            map_result = await service.read_document_map(
                session, document.id, view="outline"
            )
            payload = copy.deepcopy(
                (await session.get(DocumentVersion, document.current_version_id))
                .structured_content
            )
            anchor = build_document_map(payload, version_id=document.current_version_id)["blocks"][0]["id"]
            block_result = await service.read_document_block(
                session, document.id, block_id=anchor
            )
        await engine.dispose()
        return map_result, block_result, payload

    map_result, block_result, payload = run(scenario())
    map_json = json.dumps(map_result, ensure_ascii=False)
    assert "适用条件一" not in map_json
    assert "def hello" not in map_json
    assert "类别|重量" not in map_json
    block_json = json.dumps(block_result, ensure_ascii=False)
    # Block response only contains the requested block's text, not the rest.
    other_blocks = [
        b for b in payload["blocks"]
        if b["type"] != "heading" and b["text"] != block_result["block"]["text"]
    ]
    for other in other_blocks:
        if other["text"]:
            assert other["text"] not in block_json
