"""Tests for the chunking job wired into the worker pipeline."""

from io import BytesIO

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from apps.api.core.db import Base
from apps.api.models.blobs import Blob
from apps.api.models.chunks import DocumentChunk
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.api.parsers.base import Block, StructuredContent
from apps.api.storage.local import LocalBlobStorage
from apps.worker.core.config import settings as worker_settings
from apps.worker.services.processor import (
    CHUNKING_STAGE,
    UNDERSTANDING_IDEMPOTENCY,
    UNDERSTANDING_STAGE,
    _clear_understanding_results,
    _process_chunking,
    _release_webdav_source_blob,
    process_single_job,
)


@pytest.fixture
def engine():
    return create_engine("sqlite:///:memory:")


@pytest.fixture
def session(engine):
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        yield session


def _make_structured_payload():
    structured = StructuredContent(
        document_type="markdown",
        blocks=[
            Block(
                type="heading",
                text="引言",
                heading_path=["引言"],
                level=1,
                paragraph_index=0,
            ),
            Block(
                type="paragraph",
                text="一句话介绍本节内容。",
                heading_path=["引言"],
                paragraph_index=1,
            ),
        ],
    )
    return structured.to_dict()


class TestChunkingJob:
    def test_webdav_original_is_released_after_parsing(
        self, session, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(worker_settings, "storage_path", str(tmp_path))
        storage = LocalBlobStorage(tmp_path)
        stored = storage.save(BytesIO(b"# source"), max_bytes=1024)
        blob = Blob(
            sha256=stored.sha256,
            storage_key=stored.storage_key,
            content_type="text/markdown",
            file_size=stored.size,
            original_filename="source.md",
        )
        document = Document(
            title="WebDAV 临时文件",
            source_type=DocumentSourceType.file,
            meta={"external_source": "webdav"},
        )
        session.add_all([blob, document])
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            blob_id=blob.id,
            version_number=1,
            content_hash=stored.sha256,
            raw_content="source",
            structured_content=_make_structured_payload(),
            processing_status="ready",
            meta={"external_source": "webdav"},
        )
        session.add(version)
        session.commit()

        _release_webdav_source_blob(session, version)

        session.refresh(version)
        assert version.blob_id is None
        assert session.get(Blob, blob.id) is None
        assert not (tmp_path / stored.storage_key).exists()

    def test_process_chunking_persists_parent_and_children(self, session):
        document = Document(
            title="测试文档", source_type=DocumentSourceType.note
        )
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="seed",
            raw_content="引言\n一句话介绍本节内容。",
            structured_content=_make_structured_payload(),
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        job = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage=CHUNKING_STAGE,
            idempotency_key=f"{version.id}:{CHUNKING_STAGE}:chunking:m3-v1",
            config_version="chunking:m3-v1",
        )
        session.add(job)
        session.commit()

        assert _process_chunking(session, job, document, version) is True
        session.refresh(job)
        assert job.status == "completed"

        rows = session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == version.id
            ).order_by(DocumentChunk.order_index)
        ).all()
        roles = [row.role for row in rows]
        assert roles == ["parent", "child"]
        child = rows[1]
        assert child.parent_id == rows[0].id
        assert child.content_hash
        assert child.heading_path == ["引言"]
        assert child.char_count > 0
        assert child.token_estimate > 0
        assert child.is_current is True
        session.refresh(version)
        assert version.meta["document_profile"]["detected_type"] == "general"
        assert version.meta["chunking_config"]["profile_version"] == "chunk-profile:v1"
        assert child.extra["document_profile"]["profile_version"] == "chunk-profile:v1"

    def test_chunking_is_idempotent(self, session):
        document = Document(
            title="幂等测试", source_type=DocumentSourceType.note
        )
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="seed",
            structured_content=_make_structured_payload(),
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        job = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage=CHUNKING_STAGE,
            idempotency_key=f"{version.id}:{CHUNKING_STAGE}:chunking:m3-v1",
            config_version="chunking:m3-v1",
        )
        session.add(job)
        session.commit()

        _process_chunking(session, job, document, version)
        first = session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == version.id
            )
        ).all()

        job.status = "created"
        job.finished_at = None
        session.add(job)
        session.commit()
        assert _process_chunking(session, job, document, version) is True
        second = session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == version.id
            )
        ).all()
        # Re-running chunking should not produce duplicates.
        assert len(first) == len(second)
        assert {row.content_hash for row in first} == {
            row.content_hash for row in second
        }

    def test_new_version_retires_previous_version_chunks(self, session):
        document = Document(title="版本更新", source_type=DocumentSourceType.file)
        session.add(document)
        session.flush()
        versions = []
        for number in (1, 2):
            version = DocumentVersion(
                document_id=document.id,
                version_number=number,
                content_hash=f"seed-{number}",
                structured_content=_make_structured_payload(),
                processing_status="ready",
            )
            session.add(version)
            session.flush()
            job = ProcessingJob(
                document_id=document.id,
                document_version_id=version.id,
                stage=CHUNKING_STAGE,
                idempotency_key=f"{version.id}:{CHUNKING_STAGE}:version-test",
                config_version="version-test",
            )
            session.add(job)
            session.commit()
            assert _process_chunking(session, job, document, version) is True
            versions.append(version)

        old_chunks = session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == versions[0].id
            )
        ).all()
        new_chunks = session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == versions[1].id
            )
        ).all()
        assert old_chunks and all(chunk.is_current is False for chunk in old_chunks)
        assert new_chunks and all(chunk.is_current is True for chunk in new_chunks)

    def test_processing_pipeline_enqueues_chunking_after_parse(self, session):
        document = Document(
            title="完整流程", source_type=DocumentSourceType.note
        )
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="abc",
            raw_content="# 标题\n第一段。",
            processing_status="created",
        )
        session.add(version)
        session.flush()
        parsing_job = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage="parsing",
            idempotency_key=f"{document.id}:{version.id}:parsing:v1",
            config_version="1",
        )
        session.add(parsing_job)
        session.commit()

        assert process_single_job(session, parsing_job.id) is True

        chunking_job = session.scalar(
            select(ProcessingJob).where(
                ProcessingJob.stage == CHUNKING_STAGE,
                ProcessingJob.document_version_id == version.id,
            )
        )
        assert chunking_job is not None
        assert process_single_job(session, chunking_job.id) is True
        session.refresh(version)
        assert version.processing_status == "ready"
        assert version.meta.get("chunk_count", 0) >= 1

        rows = session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == version.id
            )
        ).all()
        assert any(row.role == "child" for row in rows)

    def test_process_chunking_marks_failed_when_no_structured(self, session):
        document = Document(
            title="缺少结构化", source_type=DocumentSourceType.note
        )
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="seed",
            structured_content=None,
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        job = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage=CHUNKING_STAGE,
            idempotency_key=f"{version.id}:{CHUNKING_STAGE}:chunking:m3-v1",
            config_version="chunking:m3-v1",
        )
        session.add(job)
        session.commit()

        assert _process_chunking(session, job, document, version) is False
        session.refresh(job)
        session.refresh(version)
        assert job.status == "failed"
        assert "缺少结构化" in (job.last_error or "")
        assert version.processing_status == "failed"

    def test_reprocess_resets_understanding_and_chunking_jobs(self, session):
        document = Document(
            title="重新处理", source_type=DocumentSourceType.note
        )
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="seed",
            structured_content=_make_structured_payload(),
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        understanding = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage=UNDERSTANDING_STAGE,
            status="completed",
            idempotency_key=(
                f"{version.id}:{UNDERSTANDING_STAGE}:{UNDERSTANDING_IDEMPOTENCY}"
            ),
            config_version=UNDERSTANDING_IDEMPOTENCY,
        )
        chunking = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage=CHUNKING_STAGE,
            status="completed",
            idempotency_key=f"{version.id}:{CHUNKING_STAGE}:chunking:m3-v1",
            config_version="chunking:m3-v1",
        )
        session.add_all([understanding, chunking])
        session.commit()

        _clear_understanding_results(session, version.id)

        assert understanding.status == "created"
        assert chunking.status == "created"


class TestChunkerIntegration:
    """Cross-checks between the chunker and the persistence path."""

    def test_persisted_search_text_includes_title_and_heading(self, session):
        document = Document(
            title="藏知路线图", source_type=DocumentSourceType.markdown
            if hasattr(DocumentSourceType, "markdown")
            else DocumentSourceType.note,
        )
        session.add(document)
        session.flush()
        structured = StructuredContent(
            document_type="markdown",
            blocks=[
                Block(
                    type="heading",
                    text="搜索",
                    heading_path=["搜索"],
                    level=1,
                    paragraph_index=0,
                ),
                Block(
                    type="paragraph",
                    text="搜索应该能找到相关片段。",
                    heading_path=["搜索"],
                    paragraph_index=1,
                ),
            ],
        )
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="seed",
            structured_content=structured.to_dict(),
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        job = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage=CHUNKING_STAGE,
            idempotency_key=f"{version.id}:{CHUNKING_STAGE}:chunking:m3-v1",
            config_version="chunking:m3-v1",
        )
        session.add(job)
        session.commit()

        _process_chunking(session, job, document, version)
        rows = session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == version.id,
                DocumentChunk.role == "parent",
            )
        ).all()
        assert rows
        # Title and heading path should be in the searchable text for
        # the parent chunk.
        assert "藏知路线图" in rows[0].search_text
        assert "搜索" in rows[0].search_text
