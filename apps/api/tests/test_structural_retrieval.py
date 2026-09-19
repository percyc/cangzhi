"""Candidate lexical/context integration; not production vector quality metrics."""
import asyncio

import pytest

from apps.api.models.chunks import DocumentChunk
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.parsers.base import Block, StructuredContent
from apps.api.services.chunking_structural import build_structural_candidate
from apps.api.services.search import _search_documents_lexical, _attach_neighbor_context
from apps.api.tests.test_search import search_db  # noqa: F401 - isolated SQLite fixture


@pytest.mark.parametrize("kind", ["pdf", "docx", "markdown", "txt"])
def test_candidate_search_preserves_condition_and_source_location(search_db, kind):
    async def run():
        source = ("Background details. " * 70
                  + "Renewal requires written approval within 30 days. "
                  + "Additional notes. " * 60)
        payload = StructuredContent(document_type=kind, blocks=[
            Block("paragraph", source, [], page=3, paragraph_index=12)
        ]).to_dict()
        candidate = build_structural_candidate(payload)
        assert candidate["source_content_complete"]
        assert candidate["unverified_spans"] == 0
        async with search_db() as db:
            document = Document(title="Manual", source_type=DocumentSourceType.file)
            db.add(document)
            await db.flush()
            version = DocumentVersion(document_id=document.id, version_number=1,
                content_hash="test-source", raw_content=source, structured_content=payload,
                processing_status="ready")
            db.add(version)
            await db.flush()
            document.current_version_id = version.id
            parents = {}
            by_id = {}
            for spec in candidate["specs"]:
                values = {key: value for key, value in spec.items() if key != "parent_external_id"}
                chunk = DocumentChunk(**values, document_id=document.id,
                    document_version_id=version.id, search_text=spec["content"],
                    parent_id=parents.get(spec["parent_external_id"]), is_current=True)
                db.add(chunk)
                await db.flush()
                by_id[chunk.id] = chunk
                if chunk.role == "parent":
                    parents[chunk.external_id] = chunk.id
            await db.commit()
            result = await _search_documents_lexical(db, query="renewal", limit=5)
            await _attach_neighbor_context(db, result.hits)
            assert len(result.hits) == 1
            hit = result.hits[0]
            assert hit.document_id == document.id
            assert "written approval within 30 days" in hit.context
            chunk = by_id[hit.chunk_id]
            extra = chunk.extra or {}
            start = extra.get("core_source_start", chunk.source_start)
            end = extra.get("core_source_end", chunk.source_end)
            prefix = extra.get("overlap_prefix_chars", 0)
            core = chunk.content[prefix + 2:] if prefix else chunk.content
            assert source.strip()[start:end] == core
            assert chunk.page == 3 and chunk.paragraph_index == 12
    asyncio.run(run())
