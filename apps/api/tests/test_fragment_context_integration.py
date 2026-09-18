"""Shared retrieval recovery, isolation and evidence-budget regressions."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import pytest

from sqlalchemy import select
from apps.api.models.chunks import DocumentChunk
from apps.api.models.documents import Document, DocumentVersion
from apps.api.services.fragment_context import recover_fragment_contexts
from apps.api.services.qa import QAService
from apps.api.services.qa import AskRequest
from apps.api.services.deep_analysis import DeepAnalysisService, AgentDecision, EvidenceAudit, _bound_evidence
from apps.api.services.search import _attach_neighbor_context
from apps.api.services.workspaces import bind_workspace_context
from apps.api.tests.test_qa import qa_db, _seed_document, StubProvider  # noqa: F401


def test_deep_audit_explicitly_requests_json_for_compatible_channels():
    async def run():
        provider = StubProvider()
        provider.json_responses.append({"status": "sufficient", "summary": "证据充分", "next_query": None})
        result = await DeepAnalysisService(provider)._audit_evidence(
            "资料适用范围是什么？", [], unresolved_evidence=[])
        assert result.status == "sufficient"
        assert result.next_query == ""
        assert "JSON" in provider.json_calls[0]["system"]
        with pytest.raises(ValueError, match="needs_more"):
            EvidenceAudit.model_validate({"status": "needs_more", "next_query": None})
    asyncio.run(run())


async def seed(factory):
    doc_id = await _seed_document(factory, title="通用设备技术要求", body="额定速度")
    async with factory() as db:
        doc = await db.get(Document, doc_id)
        version = await db.get(DocumentVersion, doc.current_version_id)
        texts = ["适用范围", "额定速度", "25", "km/h", "类型", "A", "B", "C", "质量", "3500", "kg"]
        texts += ["设备运行条件说明。" * 24, "但通过其他等效认证的设备可以豁免。"]
        version.structured_content = {"document_type": "pdf", "blocks": [
            {"type": "paragraph", "text": t, "page": 1} for t in texts
        ] + [{"type": "paragraph", "text": "OTHER PAGE MUST NOT APPEAR", "page": 2}]}
        chunk = await db.scalar(select(DocumentChunk).where(DocumentChunk.document_id == doc_id, DocumentChunk.role == "child"))
        parent = await db.scalar(select(DocumentChunk).where(DocumentChunk.document_id == doc_id, DocumentChunk.role == "parent"))
        chunk.parent_id = parent.id
        chunk.page = 1
        await db.commit()
        return doc_id, version.id, chunk.id, "\n".join(texts)


def test_shared_search_and_qa_preserve_whole_page_and_exception(qa_db):
    async def run():
        doc_id, version_id, chunk_id, expected = await seed(qa_db)
        async with qa_db() as db:
            chunk = await db.get(DocumentChunk, chunk_id)
            hit = SimpleNamespace(chunk_id=chunk.id, parent_id=chunk.parent_id, table_location=None, context=None)
            await _attach_neighbor_context(db, [hit])
            assert hit.context == expected
            doc = await db.get(Document, doc_id)
            row = SimpleNamespace(chunk_id=chunk.id, document_id=doc_id,
                document_version_id=version_id, page=1, content=chunk.content,
                source_type=doc.source_type, source_url=None, rank=1, title=doc.title,
                heading_path=[], paragraph_index=0, source_start=0, source_end=4)
            evidence = await QAService(StubProvider())._rows_to_evidence(
                db, [row] * 8, question="设备适用范围是什么？", evidence_item_chars=80)
            assert len(evidence) == 1
            assert evidence[0].snippet == expected
            assert "豁免" in evidence[0].snippet
            duplicate = replace(evidence[0], chunk_id=chunk.id + 100)
            other_version = replace(duplicate, document_version_id=version_id + 1)
            assert len(_bound_evidence([evidence[0], duplicate, other_version])) == 2
    asyncio.run(run())


def test_recovery_rejects_wrong_version_deleted_and_other_workspace(qa_db):
    async def run():
        doc_id, version_id, chunk_id, _ = await seed(qa_db)
        async with qa_db() as db:
            chunk = await db.get(DocumentChunk, chunk_id)
            assert chunk_id in await recover_fragment_contexts(db, [chunk])
            chunk.is_current = False
            assert await recover_fragment_contexts(db, [chunk]) == {}
            chunk.is_current = True
            bind_workspace_context(db.sync_session, 999)
            assert await recover_fragment_contexts(db, [chunk]) == {}
            bind_workspace_context(db.sync_session, 1)
            doc = await db.get(Document, doc_id)
            doc.current_version_id = None
            await db.flush()
            assert await recover_fragment_contexts(db, [chunk]) == {}
            doc.current_version_id = version_id
            doc.is_deleted = True
            await db.flush()
            assert await recover_fragment_contexts(db, [chunk]) == {}
    asyncio.run(run())


def test_recovery_never_expands_dataset_or_non_pdf(qa_db):
    async def run():
        _, version_id, chunk_id, _ = await seed(qa_db)
        async with qa_db() as db:
            chunk = await db.get(DocumentChunk, chunk_id)
            chunk.extra = {"sheet_name": "Data"}
            assert await recover_fragment_contexts(db, [chunk]) == {}
            chunk.extra = {}
            version = await db.get(DocumentVersion, version_id)
            version.structured_content = {**version.structured_content, "document_type": "docx"}
            await db.flush()
            assert await recover_fragment_contexts(db, [chunk]) == {}
    asyncio.run(run())


def test_deep_read_recovers_authorized_anchor_without_changing_stored_chunk(qa_db):
    async def run():
        _, _, chunk_id, expected = await seed(qa_db)
        async with qa_db() as db:
            service = DeepAnalysisService(StubProvider())
            output, _, evidence, _ = await service._execute_action(
                db, request=AskRequest(question="设备要求是什么？"),
                decision=AgentDecision(action="read_chunk", chunk_id=chunk_id),
                allowed_dataset_documents=set(), allowed_dataset_ids=set(),
                allowed_chunk_ids={chunk_id})
            assert output["output"]["content"] == expected
            assert evidence[0].snippet == expected
            chunk = await db.get(DocumentChunk, chunk_id)
            assert chunk.content == "额定速度"
    asyncio.run(run())
