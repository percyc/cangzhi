"""Tests for the Q&A service and the /api/ask endpoint.

These tests exercise the service in isolation (with a stub provider)
and through the FastAPI app (with a real SQLite test database).
"""

from __future__ import annotations

import asyncio
from typing import Sequence

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import apps.api.models  # noqa: F401
from apps.api.ai import AIProvider, AIProviderError, AnswerResult
from apps.api.core.db import Base, get_db
from apps.api.main import app
from apps.api.models.chunks import DocumentChunk
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.table_rows import StructuredTableRow
from apps.api.models.taxonomy import Category, DocumentCategory, DocumentTag, Tag
from apps.api.parsers.base import Block, StructuredContent
from apps.api.services.chunker import build_chunk_specs, chunk_content_hash
from apps.api.services.qa import (
    AskError,
    AskRequest,
    QAService,
    _expanded_chunk_context,
    _merge_neighbor_context,
)


# --- fixtures -------------------------------------------------------------


@pytest.fixture
def qa_db(tmp_path):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def prepare():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(prepare())

    async def override_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    # The /api/ask routes are protected by ``require_admin``; inject
    # a stub admin so the test can exercise the endpoint without
    # driving the cookie flow.
    from apps.api.api.auth import require_admin

    app.dependency_overrides[require_admin] = lambda: _stub_admin()
    yield session_factory
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(require_admin, None)

    async def dispose():
        await engine.dispose()

    asyncio.run(dispose())


def _stub_admin():
    from types import SimpleNamespace

    return SimpleNamespace(id=1, username="tester", is_active=True)


class StubProvider(AIProvider):
    """A configurable AIProvider used in Q&A tests.

    The stub records each call and returns a payload picked from
    ``responses`` (FIFO) or raises ``error`` once exhausted. Setting
    ``is_configured_flag`` controls :meth:`is_configured` without
    touching the global settings module.
    """

    name = "stub"

    def __init__(self) -> None:
        self.responses: list[AnswerResult] = []
        self.errors: list[Exception] = []
        self.calls: list[dict] = []
        self.json_calls: list[dict] = []
        self.json_responses: list[dict] = []
        self._configured = True

    @property
    def is_configured_flag(self) -> bool:
        return self._configured

    def set_configured(self, value: bool) -> None:
        self._configured = value

    def queue(self, response: AnswerResult) -> None:
        self.responses.append(response)

    def fail_with(self, exc: Exception) -> None:
        self.errors.append(exc)

    def is_configured(self) -> bool:
        return self._configured

    def generate_understanding(self, **_):  # pragma: no cover - unused here
        raise NotImplementedError

    def generate_json(self, *, system: str, prompt: str) -> dict:
        self.json_calls.append({"system": system, "prompt": prompt})
        if not self.json_responses:
            raise AIProviderError("no queued json response")
        return self.json_responses.pop(0)

    def answer_question(
        self,
        *,
        question: str,
        evidence: Sequence[dict],
    ) -> AnswerResult:
        self.calls.append({"question": question, "evidence": list(evidence)})
        if self.errors:
            raise self.errors.pop(0)
        if not self.responses:
            raise AIProviderError("no queued response")
        return self.responses.pop(0)


async def _seed_document(
    session_factory,
    *,
    title: str,
    body: str,
    heading: str | None = None,
    category_slug: str | None = None,
    tag_slug: str | None = None,
    source_type=DocumentSourceType.note,
):
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
    blocks.append(
        Block(
            type="paragraph",
            text=body,
            heading_path=[heading] if heading else [],
            paragraph_index=0,
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
        document.current_version_id = version.id
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
            tag = await session.scalar(
                select(Tag).where(Tag.slug == tag_slug)
            )
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
        for spec in specs:
            search_text = "\n".join(
                [
                    title,
                    " > ".join(spec.heading_path or []),
                    spec.content,
                ]
            ).strip()
            session.add(
                DocumentChunk(
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
            )
        await session.commit()
    return document.id


def test_neighbor_context_keeps_previous_and_following_meaning_without_duplicates():
    previous = "前置条件：用户必须完成实名认证。"
    current = "用户必须完成实名认证。\n\n满足条件后可以提交申请。"
    following = "满足条件后可以提交申请。\n\n申请将在三个工作日内审核。"

    context = _merge_neighbor_context(previous, current, following)

    assert "前置条件" in context
    assert "三个工作日内审核" in context
    assert context.count("用户必须完成实名认证") == 1
    assert context.count("满足条件后可以提交申请") == 1


def test_neighbor_context_is_loaded_from_same_parent(qa_db):
    async def _run():
        async with qa_db() as session:
            document = Document(title="申请流程", source_type=DocumentSourceType.note)
            session.add(document)
            await session.flush()
            version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                content_hash="neighbor-test",
                processing_status="ready",
            )
            session.add(version)
            await session.flush()
            document.current_version_id = version.id

            def chunk(*, external_id, role, order, content, parent_id=None):
                return DocumentChunk(
                    document_id=document.id,
                    document_version_id=version.id,
                    parent_id=parent_id,
                    external_id=external_id,
                    role=role,
                    chunk_type="paragraph",
                    order_index=order,
                    content=content,
                    search_text=content,
                    content_hash=chunk_content_hash(content),
                    heading_path=[],
                    char_count=len(content),
                    token_estimate=len(content),
                    is_current=True,
                )

            parent = chunk(
                external_id="parent",
                role="parent",
                order=0,
                content="完整申请流程",
            )
            session.add(parent)
            await session.flush()
            children = [
                chunk(
                    external_id=f"child-{index}",
                    role="child",
                    order=index,
                    content=content,
                    parent_id=parent.id,
                )
                for index, content in enumerate(
                    ["先实名认证。", "然后提交申请。", "最后等待审核。"]
                )
            ]
            session.add_all(children)
            await session.commit()
            return await _expanded_chunk_context(
                session,
                children[1],
                fallback=children[1].content,
            )

    context = asyncio.run(_run())
    assert context == "先实名认证。\n\n然后提交申请。\n\n最后等待审核。"


def test_qa_service_returns_cited_answer(qa_db):
    async def _run():
        await _seed_document(
            qa_db,
            title="向量检索",
            body="向量检索会把文档转换为向量再做相似度比较。",
            heading="简介",
        )
        provider = StubProvider()
        provider.queue(
            AnswerResult(
                answer="向量检索会把文档转换为向量再做相似度比较。",
                citation_ids=[1],
                insufficient_evidence=False,
            )
        )
        async with qa_db() as session:
            service = QAService(provider)
            return await service.ask(
                session,
                AskRequest(question="什么是向量检索？"),
            )

    result = asyncio.run(_run())
    assert result.insufficient_evidence is False
    assert len(result.citations) == 1
    citation = result.citations[0]
    assert citation["document_id"]
    assert citation["title"] == "向量检索"
    assert "向量检索" in citation["snippet"]


def test_qa_service_uses_exact_structured_table_calculation(qa_db):
    async def _run():
        async with qa_db() as session:
            document = Document(title="采购明细", source_type=DocumentSourceType.file)
            session.add(document)
            await session.flush()
            version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                content_hash="table-question",
                processing_status="ready",
            )
            session.add(version)
            await session.flush()
            document.current_version_id = version.id
            chunk = DocumentChunk(
                document_id=document.id,
                document_version_id=version.id,
                external_id="table-child",
                role="child",
                chunk_type="table",
                order_index=0,
                content="行 2｜品类=水果｜金额=1200\n行 3｜品类=水果｜金额=800",
                search_text="采购明细 水果 金额 1200 800",
                content_hash="table-chunk",
                heading_path=["明细"],
                char_count=40,
                token_estimate=20,
                extra={
                    "sheet_name": "明细",
                    "region_index": 1,
                    "row_start": 2,
                    "row_end": 3,
                    "column_names": ["品类", "金额"],
                },
                is_current=True,
            )
            session.add(chunk)
            session.add_all(
                [
                    StructuredTableRow(
                        document_id=document.id,
                        document_version_id=version.id,
                        sheet_name="明细",
                        region_index=1,
                        row_number=2,
                        values={"品类": "水果", "金额": "1200"},
                    ),
                    StructuredTableRow(
                        document_id=document.id,
                        document_version_id=version.id,
                        sheet_name="明细",
                        region_index=1,
                        row_number=3,
                        values={"品类": "水果", "金额": "800"},
                    ),
                ]
            )
            await session.commit()
            document_id = document.id

        provider = StubProvider()
        provider.json_responses.append(
            {
                "document_id": document_id,
                "sheet_name": "明细",
                "region_index": 1,
                "filters": [
                    {"column": "品类", "operator": "eq", "value": "水果"}
                ],
                "group_by": [],
                "metric": "sum",
                "metric_column": "金额",
                "sort_by": "metric",
                "sort_order": "desc",
                "limit": 10,
            }
        )
        provider.queue(
            AnswerResult(
                answer="水果采购金额合计为 2000。",
                citation_ids=[1],
                insufficient_evidence=False,
            )
        )
        async with qa_db() as session:
            result = await QAService(provider).ask(
                session,
                AskRequest(
                    question="水果的采购金额合计是多少？",
                    document_ids=[document_id],
                ),
            )
        return result, provider

    result, provider = asyncio.run(_run())
    assert result.retrieval["mode"] == "structured_table"
    assert result.retrieval["structured_table"]["matched_rows"] == 2
    assert '"metric": 2000' in provider.calls[0]["evidence"][0]["snippet"]
    assert result.citations[0]["table_location"]["row_start"] == 2
    assert result.citations[0]["table_location"]["row_end"] == 3


def test_qa_service_skips_model_when_no_evidence(qa_db):
    async def _run():
        provider = StubProvider()
        service = QAService(provider)
        async with qa_db() as session:
            return await service.ask(
                session,
                AskRequest(question="完全找不到的内容 xyzqwerty"),
            ), provider

    result, provider = asyncio.run(_run())
    assert result.insufficient_evidence is True
    assert result.citations == []
    assert provider.calls == []  # we must never call the model


def test_qa_service_raises_when_provider_not_configured(qa_db):
    async def _run():
        provider = StubProvider()
        provider.set_configured(False)
        await _seed_document(
            qa_db,
            title="向量检索",
            body="向量检索会把文档转换为向量再做相似度比较。",
        )
        service = QAService(provider)
        async with qa_db() as session:
            return await service.ask(
                session,
                AskRequest(question="什么是向量检索？"),
            )

    with pytest.raises(AskError) as exc_info:
        asyncio.run(_run())
    assert exc_info.value.code == "provider_not_configured"


def test_qa_service_propagates_provider_failure_as_user_facing_error(qa_db):
    async def _run():
        await _seed_document(
            qa_db,
            title="向量检索",
            body="向量检索会把文档转换为向量再做相似度比较。",
        )
        provider = StubProvider()
        provider.fail_with(AIProviderError("boom"))
        service = QAService(provider)
        async with qa_db() as session:
            return await service.ask(
                session,
                AskRequest(question="什么是向量检索？"),
            )

    with pytest.raises(AskError) as exc_info:
        asyncio.run(_run())
    assert exc_info.value.code == "provider_failed"
    # Make sure the user-facing message does not leak internal details.
    assert "boom" not in str(exc_info.value)


def test_qa_service_recalls_chinese_natural_question(qa_db):
    async def _run():
        await _seed_document(
            qa_db,
            title="向量检索笔记",
            body="向量检索把文档转换为向量再比较相似度。",
            heading="基本概念",
        )
        await _seed_document(
            qa_db,
            title="做饭心得",
            body="今天学做了一道新菜，味道不错。",
        )
        provider = StubProvider()
        provider.queue(
            AnswerResult(
                answer="向量检索把文档转换为向量再做相似度比较。",
                citation_ids=[1],
                insufficient_evidence=False,
            )
        )
        async with qa_db() as session:
            service = QAService(provider)
            return await service.ask(
                session,
                AskRequest(question="我想了解向量检索的原理"),
            )

    result = asyncio.run(_run())
    assert result.evidence
    assert result.evidence[0].title == "向量检索笔记"
    assert result.insufficient_evidence is False
    assert result.citations[0]["title"] == "向量检索笔记"


def test_qa_service_respects_category_filter(qa_db):
    async def _run():
        await _seed_document(
            qa_db,
            title="技术：向量检索",
            body="向量检索把文档转换为向量再比较相似度。",
            category_slug="tech",
        )
        await _seed_document(
            qa_db,
            title="生活：做饭笔记",
            body="今天学做了一道新菜。",
            category_slug="life",
        )
        provider = StubProvider()
        provider.queue(
            AnswerResult(
                answer="生活类的资料没有向量检索。",
                citation_ids=[],
                insufficient_evidence=True,
            )
        )
        async with qa_db() as session:
            service = QAService(provider)
            return await service.ask(
                session,
                AskRequest(
                    question="向量检索的原理",
                    category_slugs=["life"],
                ),
            )

    result = asyncio.run(_run())
    # Filter should exclude the tech document, leaving no evidence, so
    # the model is never called.
    assert result.insufficient_evidence is True
    assert result.citations == []
    assert result.evidence == []


def test_qa_service_enforces_question_length(qa_db):
    async def _run():
        service = QAService(StubProvider())
        async with qa_db() as session:
            return await service.ask(
                session,
                AskRequest(question="x" * 1000),
            )

    with pytest.raises(AskError) as exc_info:
        asyncio.run(_run())
    assert exc_info.value.code == "question_too_long"


# --- API integration -----------------------------------------------------


def _override_provider(provider):
    """Patch ``build_provider_from_db`` used by the /api/ask module.

    The endpoint resolves the provider via an awaited call, not a
    ``Depends``, so we monkey-patch the symbol the module looks up.
    The same pattern lets the rest of the suite inject a fake
    provider without standing up a real model endpoint.
    """

    app.dependency_overrides.pop(_provider_dependency, None)
    original = ask_module.build_provider_from_db

    async def _fake(_db):
        return provider

    ask_module.build_provider_from_db = _fake  # type: ignore[assignment]
    return original


def _restore_provider(original):
    ask_module.build_provider_from_db = original  # type: ignore[assignment]


def test_ask_endpoint_returns_cited_answer(qa_db):
    asyncio.run(
        _seed_document(
            qa_db,
            title="向量检索",
            body="向量检索把文档转换为向量再比较相似度。",
            heading="简介",
        )
    )
    provider = StubProvider()
    provider.queue(
        AnswerResult(
            answer="向量检索把文档转换为向量再比较相似度。",
            citation_ids=[1],
            insufficient_evidence=False,
        )
    )
    original = _override_provider(provider)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/ask",
            json={"question": "什么是向量检索？"},
        )
    finally:
        _restore_provider(original)
    assert response.status_code == 200
    body = response.json()
    assert body["insufficient_evidence"] is False
    assert body["citations"][0]["title"] == "向量检索"
    assert body["evidence"][0]["document_id"]


def test_ask_endpoint_rejects_fabricated_citation(qa_db):
    asyncio.run(
        _seed_document(
            qa_db,
            title="向量检索",
            body="向量检索把文档转换为向量再比较相似度。",
        )
    )
    provider = StubProvider()
    provider.queue(
        AnswerResult(
            answer="我编造了引用",
            citation_ids=[42],
            insufficient_evidence=False,
        )
    )
    original = _override_provider(provider)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/ask",
            json={"question": "什么是向量检索？"},
        )
    finally:
        _restore_provider(original)
    assert response.status_code == 502
    body = response.json()
    assert body["detail"]["code"] == "provider_failed"


def test_ask_endpoint_returns_insufficient_without_evidence(qa_db):
    provider = StubProvider()
    original = _override_provider(provider)
    try:
        client = TestClient(app)
        response = client.post(
            "/api/ask",
            json={"question": "完全不存在的关键词 abcdef12345"},
        )
    finally:
        _restore_provider(original)
    assert response.status_code == 200
    body = response.json()
    assert body["insufficient_evidence"] is True
    assert body["citations"] == []
    # Crucially the model must not have been called.
    assert provider.calls == []


def test_ask_endpoint_returns_friendly_status_when_provider_missing(qa_db):
    from apps.api.api import ask as ask_module_local

    original = ask_module_local.build_provider_from_db

    async def _none(_db):
        return None

    ask_module_local.build_provider_from_db = _none  # type: ignore[assignment]
    try:
        client = TestClient(app)
        response = client.post(
            "/api/ask",
            json={"question": "什么是向量检索？"},
        )
    finally:
        ask_module_local.build_provider_from_db = original  # type: ignore[assignment]
    assert response.status_code == 503
    body = response.json()
    assert body["detail"]["code"] == "provider_not_configured"


def test_ask_status_endpoint(qa_db):
    from apps.api.api import ask as ask_module_local

    class _Unconfigured:
        name = "openai"
        def is_configured(self) -> bool:
            return False

    original = ask_module_local.build_provider_from_db

    async def _fake(_db):
        return _Unconfigured()

    ask_module_local.build_provider_from_db = _fake  # type: ignore[assignment]
    try:
        client = TestClient(app)
        response = client.get("/api/ask/status")
    finally:
        ask_module_local.build_provider_from_db = original  # type: ignore[assignment]
    assert response.status_code == 200
    assert response.json() == {
        "provider_configured": False,
        "provider": "openai",
    }


# Import the module-level symbol used by the override helpers.
from apps.api.api import ask as ask_module  # noqa: E402,F401

_provider_dependency = ask_module.build_provider_from_db
