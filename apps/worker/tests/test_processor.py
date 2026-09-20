import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import apps.api.models  # noqa: F401 - register all mapped tables
from apps.api.core.db import Base
from apps.api.embeddings import compute_config_fingerprint
from apps.api.embeddings.build_service import (
    EMBEDDING_STAGE,
    _idempotency_key,
)
from apps.api.embeddings.canary import CANARY_VERSION
from apps.api.embeddings.sampling import evenly_sample_chunks
from apps.api.models.blobs import Blob
from apps.api.models.chunks import DocumentChunk
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.embedding_profiles import ChunkEmbedding, EmbeddingProfile
from apps.api.models.processing import ProcessingJob
from apps.api.models.taxonomy import Category, DocumentCategory, DocumentSummary
from apps.api.models.webdav import WebDAVEntry, WebDAVSource
from apps.api.models.workspaces import Workspace
from apps.worker.core.config import settings as worker_settings
from apps.worker.services.processor import (
    _enqueue_embedding_jobs_for_new_chunks,
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
    def test_reused_worker_session_rebinds_taxonomy_to_each_workspace(self, session):
        default_workspace = Workspace(
            slug="default",
            name="默认空间",
            is_default=True,
            status="active",
            settings={},
        )
        research_workspace = Workspace(
            slug="research",
            name="研究",
            status="active",
            settings={},
        )
        session.add_all([default_workspace, research_workspace])
        session.flush()

        jobs = []
        for workspace, title in (
            (default_workspace, "默认资料"),
            (research_workspace, "研究资料"),
        ):
            document = Document(
                workspace_id=workspace.id,
                title=title,
                source_type=DocumentSourceType.note,
            )
            session.add(document)
            session.flush()
            version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                content_hash=f"hash-{workspace.slug}",
                raw_content="没有配置模型时进入收件箱",
                processing_status="ready",
            )
            session.add(version)
            session.flush()
            document.current_version_id = version.id
            job = ProcessingJob(
                document_id=document.id,
                document_version_id=version.id,
                stage="understanding",
                idempotency_key=f"{document.id}:understanding:test",
                config_version="test",
            )
            session.add(job)
            jobs.append(job)
        session.commit()

        assert process_single_job(session, jobs[0].id) is True
        assert process_single_job(session, jobs[1].id) is True

        categories = list(
            session.scalars(
                select(Category).execution_options(include_all_workspaces=True)
            ).all()
        )
        assert {(item.workspace_id, item.slug) for item in categories} == {
            (default_workspace.id, "inbox"),
            (research_workspace.id, "inbox"),
        }

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


class TestParseUnicodeSanitization:
    """The worker must normalise every derived string before persisting.

    PDF, docx and web parsers can produce blocks with NUL bytes
    or lone UTF-16 surrogates when the source is malformed. The
    worker sanitises the result so the chunker, search index and
    JSON column never see the raw bytes. This suite covers the
    end-to-end behaviour of :func:`process_single_job` plus the
    helpers it calls.
    """

    def test_note_with_unicode_dirty_text_is_sanitised_on_persist(
        self, session, monkeypatch
    ):
        from apps.api.parsers.base import Block, StructuredContent
        from apps.api.parsers import get_parser_for_content

        dirty = StructuredContent(
            document_type="note",
            blocks=[
                Block(
                    type="paragraph",
                    text="hello\ud83d world",  # lone high surrogate
                    heading_path=["中文\ud83d\ude00"],  # valid pair + repair
                    extra={"ocr_source": "local\ud83d"},
                ),
                Block(
                    type="paragraph",
                    text="valid\ud83d\ude00\ud83d",  # pair + lone
                    heading_path=[],
                    extra={"nested": {"k": "a\x00b"}},
                ),
            ],
            metadata={
                "title": "Title\ud83d",
                "regions": [
                    {"sheet_name": "Sheet\x00", "row_start": 1},
                ],
            },
        )

        class _DirtyNoteParser:
            def parse(self, _content, _content_type=None):
                from apps.api.parsers.base import ParserResult

                return ParserResult(success=True, structured_content=dirty)

        monkeypatch.setattr(
            get_parser_for_content,
            "__call__",
            lambda *args, **kwargs: _DirtyNoteParser(),
        )
        # The worker imports the symbol locally; re-patch the
        # module reference so the dispatch picks up the dirty
        # parser.
        monkeypatch.setattr(
            "apps.worker.services.processor.get_parser_for_content",
            lambda *args, **kwargs: _DirtyNoteParser(),
        )

        doc = Document(title="dirty", source_type=DocumentSourceType.note)
        session.add(doc)
        session.flush()
        version = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            content_hash="dirty",
            raw_content="placeholder",
            processing_status="created",
        )
        session.add(version)
        session.flush()
        job = ProcessingJob(
            document_id=doc.id,
            document_version_id=version.id,
            stage="parsing",
            idempotency_key=f"{doc.id}:{version.id}:parsing:dirty-unicode",
            config_version="1",
        )
        session.add(job)
        session.commit()

        assert process_single_job(session, job.id) is True
        session.refresh(version)
        session.refresh(job)

        assert job.status == "completed"
        assert version.processing_status == "ready"

        # Derived text uses the sanitised representation.
        assert "\ud83d" not in (version.raw_content or "")
        assert "\ude00" not in (version.raw_content or "")
        assert "\U0001f600" in (version.raw_content or "")
        assert "hello\ufffd world" in (version.raw_content or "")
        assert "a\ufffdb" not in (version.raw_content or "")  # NUL gone

        # The persisted structured payload is clean too.
        payload = version.structured_content
        assert payload is not None
        for block in payload["blocks"]:
            assert "\ud83d" not in block["text"]
            assert "\ude00" not in block["text"]
            assert "\x00" not in block["text"]
        for heading in payload["blocks"][0]["heading_path"]:
            assert "\ud83d" not in heading
            assert "\ude00" not in heading

        # The aggregate sanitisation report is recorded in
        # metadata. No content is included; only counts.
        meta = payload["metadata"]
        sanitisation = meta.get("sanitization", {}).get("unicode")
        assert sanitisation is not None
        assert sanitisation["nul_removed"] >= 1
        assert sanitisation["surrogate_pairs_repaired"] >= 1
        assert sanitisation["lone_surrogates_replaced"] >= 1
        assert sanitisation["strings_visited"] >= 1
        # ``Title\ud83d`` was used as the document title, so it
        # is also clean.
        assert "\ud83d" not in (doc.title or "")

    def test_apply_parse_result_stamps_report_when_report_provided(
        self, session
    ):
        from apps.api.parsers.base import Block, StructuredContent
        from apps.api.parsers.text_sanitize import UnicodeSanitizationReport
        from apps.worker.services.processor import _apply_parse_result

        structured = StructuredContent(
            document_type="txt",
            blocks=[
                Block(
                    type="paragraph",
                    text="hello",
                    heading_path=[],
                )
            ],
            metadata={"title": "T"},
        )
        doc = Document(title="stale", source_type=DocumentSourceType.file)
        session.add(doc)
        session.flush()
        version = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            content_hash="x",
            raw_content="",
            processing_status="created",
        )
        session.add(version)
        session.flush()

        report = UnicodeSanitizationReport(
            nul_removed=2,
            surrogate_pairs_repaired=1,
            lone_surrogates_replaced=3,
            strings_visited=10,
        )
        _apply_parse_result(
            session,
            doc,
            version,
            structured,
            sanitization_report=report,
        )
        session.commit()
        session.refresh(version)

        stored = version.structured_content
        assert stored["metadata"]["sanitization"]["unicode"] == report.as_dict()
        assert version.processing_status == "ready"
        assert doc.title == "T"

    def test_apply_parse_result_omits_block_when_report_is_none(
        self, session
    ):
        from apps.api.parsers.base import Block, StructuredContent
        from apps.worker.services.processor import _apply_parse_result

        structured = StructuredContent(
            document_type="txt",
            blocks=[Block(type="paragraph", text="hi", heading_path=[])],
            metadata={"title": "Untouched"},
        )
        doc = Document(title="stale", source_type=DocumentSourceType.file)
        session.add(doc)
        session.flush()
        version = DocumentVersion(
            document_id=doc.id,
            version_number=1,
            content_hash="x",
            raw_content="",
            processing_status="created",
        )
        session.add(version)
        session.flush()

        _apply_parse_result(session, doc, version, structured)
        session.commit()
        session.refresh(version)

        stored = version.structured_content
        assert "sanitization" not in (stored.get("metadata") or {})


class TestWordPreviewRollbackIsolation:
    def test_parse_failure_rolls_back_before_recording_diagnostic(self, session, monkeypatch):
        from apps.worker.services import processor as proc

        document = Document(title="note", source_type=DocumentSourceType.note)
        session.add(document)
        session.flush()
        version = DocumentVersion(document_id=document.id, version_number=1,
                                  content_hash="source", raw_content="original note")
        session.add(version)
        session.flush()
        job = ProcessingJob(document_id=document.id, document_version_id=version.id,
                            stage="parsing", idempotency_key="rollback-test", config_version="1")
        session.add(job)
        session.commit()

        def fail_body(session, document, version, structured, **kwargs):
            version.content_hash = None
            session.flush()

        monkeypatch.setattr(proc, "_apply_parse_result", fail_body)
        assert not process_single_job(session, job.id)
        session.refresh(job)
        session.refresh(version)
        assert job.status == "retry"
        assert job.error_details == {"exception_class": "IntegrityError"}
        assert version.raw_content == "original note"
        assert version.content_hash == "source"

    def test_real_constraint_failure_keeps_body_and_cleans_preview(
        self, session, tmp_path, monkeypatch
    ):
        from sqlalchemy import event
        from subprocess import CompletedProcess
        from apps.worker.services import processor as proc

        document = Document(title="preview", source_type=DocumentSourceType.file)
        session.add(document)
        session.flush()
        version = DocumentVersion(document_id=document.id, version_number=1,
                                  content_hash="original-source", processing_status="ready")
        session.add(version)
        session.commit()
        version.raw_content = "parsed body"

        def fake_run(args, **kwargs):
            (Path(args[args.index("--outdir") + 1]) / "source.pdf").write_bytes(b"%PDF-test")
            return CompletedProcess(args, 0, b"", b"")

        def fail_new_blob(session, context, instances):
            for obj in session.new:
                if isinstance(obj, Blob):
                    obj.sha256 = None  # Real NOT NULL violation inside the savepoint.

        monkeypatch.setattr(proc.settings, "storage_path", str(tmp_path))
        monkeypatch.setattr(proc.shutil, "which", lambda name: "/test/soffice")
        monkeypatch.setattr(proc.subprocess, "run", fake_run)
        event.listen(session, "before_flush", fail_new_blob)
        try:
            assert not proc._ensure_word_pdf_preview(
                session, version, b"source bytes", "application/msword", "source.doc"
            )
            assert session.is_active
            session.commit()
            session.refresh(version)
            assert version.raw_content == "parsed body"
            assert version.content_hash == "original-source"
            assert version.preview_blob_id is None
            assert version.meta["preview"]["reason"] == "IntegrityError"
            assert "INSERT" not in version.meta["preview"]["message"]
            assert session.query(Blob).count() == 0
            assert not [p for p in tmp_path.rglob("*") if p.is_file()]
        finally:
            event.remove(session, "before_flush", fail_new_blob)

    def test_pending_body_failure_propagates_before_savepoint(self, session, monkeypatch):
        from sqlalchemy.exc import IntegrityError
        from apps.worker.services import processor as proc

        document = Document(title="preview", source_type=DocumentSourceType.file)
        session.add(document)
        session.flush()
        version = DocumentVersion(document_id=document.id, version_number=1,
                                  content_hash="source", processing_status="ready")
        session.add(version)
        session.commit()
        version_id = version.id
        version.content_hash = None
        monkeypatch.setattr(proc.shutil, "which", lambda name: "/test/soffice")
        with pytest.raises(IntegrityError):
            proc._ensure_word_pdf_preview(
                session, version, b"source", "application/msword", "source.doc"
            )
        session.rollback()
        assert session.get(DocumentVersion, version_id).content_hash == "source"


# --- _enqueue_embedding_jobs_for_new_chunks -----------------------------


def _make_embedding_profile(
    session,
    *,
    status: str = "active",
    base_url: str = "https://embed.example.com/v1",
) -> EmbeddingProfile:
    fingerprint = compute_config_fingerprint(
        provider="openai",
        base_url=base_url,
        model="m",
        dim=4,
        has_api_key=False,
        key_fingerprint=None,
        canary_version=CANARY_VERSION,
    )
    profile = EmbeddingProfile(
        provider="openai",
        base_url=base_url,
        model="m",
        dim=4,
        has_api_key=False,
        key_fingerprint=None,
        config_fingerprint=fingerprint,
        status=status,
        total_chunks=0,
        completed_chunks=0,
        failed_chunks=0,
    )
    session.add(profile)
    session.flush()
    return profile


def _make_child_chunks(
    session,
    *,
    version: DocumentVersion,
    count: int,
    content_hash_prefix: str = "h",
) -> list[DocumentChunk]:
    document_id = version.document_id
    chunks: list[DocumentChunk] = []
    for index in range(count):
        chunk = DocumentChunk(
            document_id=document_id,
            document_version_id=version.id,
            external_id=f"chunk-{index}",
            role="child",
            chunk_type="paragraph",
            order_index=index,
            content=f"chunk {index}",
            search_text=f"chunk {index}",
            content_hash=f"{content_hash_prefix}-{index}",
            char_count=10,
            token_estimate=2,
            is_current=True,
        )
        session.add(chunk)
        chunks.append(chunk)
    session.flush()
    return chunks


class TestEnqueueEmbeddingJobsForNewChunks:
    def test_no_profile_is_a_noop(self, session):
        document = Document(title="空", source_type=DocumentSourceType.note)
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="doc-hash",
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        _make_child_chunks(session, version=version, count=3)
        session.commit()

        _enqueue_embedding_jobs_for_new_chunks(session, document, version)
        session.commit()

        jobs = list(
            session.scalars(
                select(ProcessingJob).where(
                    ProcessingJob.stage == EMBEDDING_STAGE
                )
            ).all()
        )
        assert jobs == []

    def test_active_profile_enqueues_for_every_current_child(self, session):
        document = Document(title="active", source_type=DocumentSourceType.note)
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="doc-hash",
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        _make_child_chunks(session, version=version, count=3)
        profile = _make_embedding_profile(session, status="active")
        session.commit()

        _enqueue_embedding_jobs_for_new_chunks(session, document, version)
        session.commit()

        session.refresh(profile)
        assert profile.status == "active"
        assert profile.build_finished_at is None
        jobs = list(
            session.scalars(
                select(ProcessingJob).where(
                    ProcessingJob.embedding_profile_id == profile.id,
                    ProcessingJob.stage == EMBEDDING_STAGE,
                )
            ).all()
        )
        assert len(jobs) == 3
        for job in jobs:
            assert job.status == "created"
            assert (
                job.idempotency_key
                == _idempotency_key(
                    profile.id, job.embedding_chunk_id, profile.config_fingerprint
                )
            )

    def test_ready_profile_is_demoted_when_new_work_arrives(self, session):
        """The chunking hook must now treat ``ready`` profiles
        like ``active``/``building``: a newly materialised child
        chunk needs a vector, the profile is demoted to
        ``building`` and its counters are recomputed."""

        document = Document(title="ready", source_type=DocumentSourceType.note)
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="doc-hash",
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        chunks = _make_child_chunks(session, version=version, count=2)
        profile = _make_embedding_profile(session, status="ready")
        # The first chunk is already covered: stored vector
        # matches the live content hash and a completed job
        # already exists so the helper does not re-enqueue it.
        session.add(
            ChunkEmbedding(
                profile_id=profile.id,
                chunk_id=chunks[0].id,
                content_hash=chunks[0].content_hash,
                vector=[0.1, 0.2, 0.3, 0.4],
            )
        )
        existing_key = _idempotency_key(
            profile.id, chunks[0].id, profile.config_fingerprint
        )
        session.add(
            ProcessingJob(
                document_id=document.id,
                document_version_id=version.id,
                stage=EMBEDDING_STAGE,
                status="completed",
                idempotency_key=existing_key,
                config_version=profile.config_fingerprint,
                embedding_profile_id=profile.id,
                embedding_chunk_id=chunks[0].id,
            )
        )
        profile.total_chunks = 2
        profile.completed_chunks = 1
        profile.failed_chunks = 0
        profile.build_started_at = None
        profile.build_finished_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
        session.add(profile)
        session.commit()

        _enqueue_embedding_jobs_for_new_chunks(session, document, version)
        session.commit()
        session.refresh(profile)

        assert profile.status == "building"
        assert profile.build_finished_at is None
        # The new chunk got a fresh job; the chunk already
        # covered by a stored embedding + completed job is
        # left alone.
        jobs = list(
            session.scalars(
                select(ProcessingJob).where(
                    ProcessingJob.embedding_profile_id == profile.id,
                    ProcessingJob.stage == EMBEDDING_STAGE,
                )
            ).all()
        )
        assert len(jobs) == 2
        new_chunk_id = chunks[1].id
        enqueued_for_new = [
            job
            for job in jobs
            if job.embedding_chunk_id == new_chunk_id
        ]
        assert len(enqueued_for_new) == 1
        assert enqueued_for_new[0].status == "created"
        # Counters reflect the live corpus: only the chunks the
        # profile has any job for, with the already-embedded
        # one counted as completed.
        assert profile.total_chunks == 2
        assert profile.completed_chunks == 1

    def test_ready_profile_without_real_work_keeps_status(self, session):
        """A ``ready`` profile whose existing jobs already cover
        the new chunks must stay in ``ready``: the helper must
        not touch the profile state when nothing was enqueued."""

        document = Document(title="ready-idle", source_type=DocumentSourceType.note)
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="doc-hash",
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        chunks = _make_child_chunks(session, version=version, count=2)
        profile = _make_embedding_profile(session, status="ready")
        # Pre-populate a job for every chunk so the helper
        # cannot create a duplicate and never flips the profile.
        for chunk in chunks:
            existing_key = _idempotency_key(
                profile.id, chunk.id, profile.config_fingerprint
            )
            session.add(
                ProcessingJob(
                    document_id=document.id,
                    document_version_id=version.id,
                    stage=EMBEDDING_STAGE,
                    status="created",
                    idempotency_key=existing_key,
                    config_version=profile.config_fingerprint,
                    embedding_profile_id=profile.id,
                    embedding_chunk_id=chunk.id,
                )
            )
        session.commit()

        _enqueue_embedding_jobs_for_new_chunks(session, document, version)
        session.commit()
        session.refresh(profile)
        assert profile.status == "ready"
        jobs = list(
            session.scalars(
                select(ProcessingJob).where(
                    ProcessingJob.embedding_profile_id == profile.id,
                    ProcessingJob.stage == EMBEDDING_STAGE,
                )
            ).all()
        )
        assert len(jobs) == 2
        assert {job.embedding_chunk_id for job in jobs} == {
            chunks[0].id,
            chunks[1].id,
        }

    @pytest.mark.parametrize("has_source_fallback", [False, True])
    def test_large_table_sampled_strategy_only_enqueues_sample(self, session, has_source_fallback):
        """When the version marks a large structured table, the
        helper enqueues jobs for the sampled slice and stamps
        the same strategy on ``version.meta`` so the API side
        :func:`_current_child_chunks` reads it back.  The number
        of jobs must match the sampled count, not the full
        child-chunk count."""

        document = Document(
            title="big table", source_type=DocumentSourceType.note
        )
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="doc-hash",
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        # 1_500 children, version says "we are a sampled table
        # with 10_000 structured rows" → triggers sampling.
        chunks = _make_child_chunks(
            session, version=version, count=1_500, content_hash_prefix="h"
        )
        if has_source_fallback:
            chunks[0].extra = {"dataset_eligible": False}
        version.meta = {
            "structured_table_row_count": 10_000,
        }
        session.add(version)
        profile = _make_embedding_profile(session, status="active")
        session.commit()

        _enqueue_embedding_jobs_for_new_chunks(session, document, version)
        session.commit()

        jobs = list(
            session.scalars(
                select(ProcessingJob).where(
                    ProcessingJob.embedding_profile_id == profile.id,
                    ProcessingJob.stage == EMBEDDING_STAGE,
                )
            ).all()
        )
        # The helper samples 256 chunks out of 1_500.
        assert len(jobs) == (1_500 if has_source_fallback else 256)
        covered_chunk_ids = {job.embedding_chunk_id for job in jobs}
        assert covered_chunk_ids.issubset({chunk.id for chunk in chunks})
        # The strategy block is stamped on the version so the
        # API path's :func:`_current_child_chunks` reuses the
        # same sampled slice.
        session.refresh(version)
        strategy = (version.meta or {}).get("embedding_strategy") or {}
        assert strategy.get("mode") == ("full" if has_source_fallback else "sampled")
        assert strategy.get("total_child_chunks") == 1_500
        assert strategy.get("selected_chunks") == (1_500 if has_source_fallback else 256)
        assert strategy.get("structured_table_rows") == 10_000

    def test_small_corpus_uses_full_strategy(self, session):
        """The opposite of the sampled path: a small version
        does not cross the threshold, every child chunk becomes
        a job and the strategy is stamped as ``full``."""

        document = Document(title="small", source_type=DocumentSourceType.note)
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="doc-hash",
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        _make_child_chunks(session, version=version, count=5)
        profile = _make_embedding_profile(session, status="active")
        session.commit()

        _enqueue_embedding_jobs_for_new_chunks(session, document, version)
        session.commit()

        jobs = list(
            session.scalars(
                select(ProcessingJob).where(
                    ProcessingJob.embedding_profile_id == profile.id,
                    ProcessingJob.stage == EMBEDDING_STAGE,
                )
            ).all()
        )
        assert len(jobs) == 5
        session.refresh(version)
        strategy = (version.meta or {}).get("embedding_strategy") or {}
        assert strategy.get("mode") == "full"
        assert strategy.get("selected_chunks") == 5
        assert strategy.get("total_child_chunks") == 5

    def test_idempotency_key_matches_api_helper(self, session):
        """The worker's auto-enqueue must use the exact same
        :func:`_idempotency_key` the API path produces so an
        operator-issued rebuild and a chunking-time enqueue
        never duplicate the same job."""

        document = Document(title="keys", source_type=DocumentSourceType.note)
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="doc-hash",
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        chunks = _make_child_chunks(session, version=version, count=2)
        profile = _make_embedding_profile(session, status="active")
        session.commit()

        _enqueue_embedding_jobs_for_new_chunks(session, document, version)
        session.commit()

        jobs = list(
            session.scalars(
                select(ProcessingJob).where(
                    ProcessingJob.embedding_profile_id == profile.id,
                    ProcessingJob.stage == EMBEDDING_STAGE,
                )
            ).all()
        )
        assert {job.idempotency_key for job in jobs} == {
            _idempotency_key(profile.id, chunk.id, profile.config_fingerprint)
            for chunk in chunks
        }
