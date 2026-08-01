import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from apps.api.core.db import Base
from apps.api.embeddings.sampling import evenly_sample_chunks
from apps.api.models.blobs import Blob
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.api.models.taxonomy import DocumentCategory, DocumentSummary
from apps.api.models.webdav import WebDAVEntry, WebDAVSource
from apps.worker.core.config import settings as worker_settings
from apps.worker.services.processor import (
    _ensure_word_pdf_preview,
    _is_terminal_parse_failure,
    _load_content_for_preview,
    calculate_next_retry_at,
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


class TestCalculateNextRetry:
    def test_first_retry(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        next_time = calculate_next_retry_at(0)
        delta = next_time - now
        assert 300 <= delta.total_seconds() <= 301

    def test_second_retry(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        next_time = calculate_next_retry_at(1)
        delta = next_time - now
        assert 600 <= delta.total_seconds() <= 601

    def test_exponential_backoff(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        assert calculate_next_retry_at(0, now=now) - now == datetime.timedelta(
            minutes=5
        )
        assert calculate_next_retry_at(1, now=now) - now == datetime.timedelta(
            minutes=10
        )
        assert calculate_next_retry_at(2, now=now) - now == datetime.timedelta(
            minutes=20
        )

    def test_spreadsheet_limit_is_not_retried(self):
        assert _is_terminal_parse_failure({"reason": "spreadsheet_limit_exceeded"})
        assert not _is_terminal_parse_failure({"reason": "xlsx_parse_failed"})

    def test_large_table_embedding_sample_is_even_and_keeps_edges(self):
        chunks = [
            SimpleNamespace(id=index + 1, order_index=index) for index in range(13_151)
        ]

        selected = evenly_sample_chunks(chunks, 256)

        assert len(selected) == 256
        assert selected[0].order_index == 0
        assert selected[-1].order_index == 13_150
        assert [item.order_index for item in selected] == sorted(
            item.order_index for item in selected
        )

    def test_small_chunk_collection_is_not_sampled(self):
        chunks = [
            SimpleNamespace(id=index + 1, order_index=index) for index in range(10)
        ]

        assert evenly_sample_chunks(chunks, 256) == chunks

    def test_word_preview_is_stored_as_pdf(self, session, tmp_path, monkeypatch):
        def fake_run(arguments, **_kwargs):
            output_directory = arguments[arguments.index("--outdir") + 1]
            Path(output_directory, "source.pdf").write_bytes(b"%PDF-1.7 preview")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(worker_settings, "storage_path", str(tmp_path))
        monkeypatch.setattr(
            "apps.worker.services.processor.shutil.which",
            lambda _name: "/usr/bin/soffice",
        )
        monkeypatch.setattr("apps.worker.services.processor.subprocess.run", fake_run)
        document = Document(title="Word", source_type=DocumentSourceType.file)
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="source",
            processing_status="created",
        )
        session.add(version)
        session.flush()

        assert _ensure_word_pdf_preview(
            session,
            version,
            b"word bytes",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "合同.docx",
        )
        session.flush()

        preview = session.get(Blob, version.preview_blob_id)
        assert preview is not None
        assert preview.content_type == "application/pdf"
        assert preview.original_filename == "合同.pdf"
        assert version.meta["preview"]["status"] == "ready"

    def test_preview_task_fetches_webdav_source_without_attaching_blob(
        self, session, monkeypatch
    ):
        source = WebDAVSource(
            name="远端资料",
            base_url="https://dav.example.test/",
            username="reader",
            password_cipher="cipher",
            root_path="/docs",
            include_extensions=[".docx"],
            ignore_patterns=[],
        )
        session.add(source)
        session.flush()
        document = Document(
            title="远端 Word",
            source_type=DocumentSourceType.file,
            meta={"external_source": "webdav", "webdav_source_id": source.id},
        )
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="remote",
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        session.add(
            WebDAVEntry(
                source_id=source.id,
                remote_path="/docs/合同.docx",
                document_id=document.id,
                state="synced",
            )
        )
        session.commit()

        async def fake_download(**_kwargs):
            return b"remote word", "application/octet-stream"

        monkeypatch.setattr(
            "apps.worker.services.processor.decrypt_secret",
            lambda _cipher: "password",
        )
        monkeypatch.setattr(
            "apps.worker.services.processor.download_file", fake_download
        )

        loaded = _load_content_for_preview(session, document, version)

        assert loaded == (
            b"remote word",
            "application/octet-stream",
            "/docs/合同.docx",
        )
        assert version.blob_id is None


class TestJobProcessing:
    def test_process_note_job(self, session):
        doc = Document(title="Test Note", source_type=DocumentSourceType.note)
        session.add(doc)
        session.flush()

        version = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            content_hash="abc123",
            raw_content="# Test\nHello world",
            processing_status="created",
        )
        session.add(version)
        session.flush()

        job = ProcessingJob(
            document_id=doc.id,
            document_version_id=version.id,
            stage="parsing",
            idempotency_key=f"{doc.id}:{version.id}:parsing:v1",
            config_version="1",
        )
        session.add(job)
        session.commit()

        result = process_single_job(session, job.id)
        assert result is True
        session.refresh(job)
        session.refresh(version)
        assert job.status == "completed"
        assert version.processing_status == "ready"
        assert version.structured_content is not None
        assert version.raw_content == "Test\nHello world"

    def test_job_not_found_returns_false(self, session):
        result = process_single_job(session, 9999)
        assert result is False

    def test_failed_note_empty_content(self, session):
        doc = Document(title="Test Empty", source_type=DocumentSourceType.note)
        session.add(doc)
        session.flush()

        version = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            content_hash="abc123",
            raw_content=None,
            processing_status="created",
        )
        session.add(version)
        session.flush()

        job = ProcessingJob(
            document_id=doc.id,
            document_version_id=version.id,
            stage="parsing",
            idempotency_key=f"{doc.id}:{version.id}:parsing:v1",
            config_version="1",
        )
        session.add(job)
        session.commit()

        result = process_single_job(session, job.id)
        assert result is False
        session.refresh(job)
        session.refresh(version)
        assert job.status == "failed"
        assert version.processing_status == "failed"

    def test_dynamic_url_without_adapter_is_marked_unsupported(
        self, session, monkeypatch
    ):
        monkeypatch.setattr(
            "apps.worker.services.processor._fetch_and_store_url",
            lambda *args: (
                b"<html><head><title>Dynamic page</title></head>"
                b"<body><div id='app'></div><script src='app.js'></script></body></html>",
                "text/html",
            ),
        )
        monkeypatch.setattr(
            "apps.worker.services.processor.extract_xinhua_html",
            lambda *args, **kwargs: None,
        )
        doc = Document(
            title="Dynamic page",
            source_type=DocumentSourceType.url,
            source_url="https://example.com/article",
        )
        session.add(doc)
        session.flush()
        version = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            content_hash="dynamic",
            processing_status="created",
        )
        session.add(version)
        session.flush()
        job = ProcessingJob(
            document_id=doc.id,
            document_version_id=version.id,
            stage="parsing",
            idempotency_key=f"{doc.id}:{version.id}:parsing:dynamic",
            config_version="2",
        )
        session.add(job)
        session.commit()

        assert process_single_job(session, job.id) is True
        session.refresh(version)
        session.refresh(job)
        assert job.status == "completed"
        assert version.processing_status == "unsupported"
        assert version.meta["extraction_status"] == "dynamic_page"
        assert (
            session.scalar(
                select(ProcessingJob).where(
                    ProcessingJob.stage == "understanding",
                    ProcessingJob.document_version_id == version.id,
                )
            )
            is None
        )

    def test_no_model_falls_back_to_inbox_idempotently(self, session, monkeypatch):
        monkeypatch.setattr(
            "apps.worker.services.processor.build_provider_from_session",
            lambda _session: None,
        )
        doc = Document(title="Fallback", source_type=DocumentSourceType.note)
        session.add(doc)
        session.flush()
        version = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            content_hash="fallback",
            raw_content="A useful note",
            processing_status="created",
        )
        session.add(version)
        session.flush()
        parsing_job = ProcessingJob(
            document_id=doc.id,
            document_version_id=version.id,
            stage="parsing",
            idempotency_key=f"{doc.id}:{version.id}:parsing:fallback",
            config_version="1",
        )
        session.add(parsing_job)
        session.commit()

        assert process_single_job(session, parsing_job.id) is True
        understanding_job = session.scalar(
            select(ProcessingJob).where(ProcessingJob.stage == "understanding")
        )
        assert understanding_job is not None
        assert process_single_job(session, understanding_job.id) is True
        assert process_single_job(session, understanding_job.id) is False

        links = session.scalars(
            select(DocumentCategory).where(
                DocumentCategory.document_version_id == version.id
            )
        ).all()
        assert len(links) == 1
        assert links[0].source == "fallback"
        assert (
            session.scalar(
                select(DocumentSummary).where(
                    DocumentSummary.document_version_id == version.id
                )
            )
            is None
        )
        session.refresh(version)
        assert version.processing_status == "ready"
        assert version.meta["ai_status"] == "not_configured"
