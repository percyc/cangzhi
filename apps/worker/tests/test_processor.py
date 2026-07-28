import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from apps.api.core.db import Base
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.api.models.taxonomy import DocumentCategory, DocumentSummary
from apps.worker.services.processor import calculate_next_retry_at, process_single_job


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
        assert calculate_next_retry_at(0, now=now) - now == datetime.timedelta(minutes=5)
        assert calculate_next_retry_at(1, now=now) - now == datetime.timedelta(minutes=10)
        assert calculate_next_retry_at(2, now=now) - now == datetime.timedelta(minutes=20)


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
        assert session.scalar(
            select(ProcessingJob).where(
                ProcessingJob.stage == "understanding",
                ProcessingJob.document_version_id == version.id,
            )
        ) is None

    def test_no_model_falls_back_to_inbox_idempotently(self, session, monkeypatch):
        monkeypatch.setattr(
            "apps.worker.services.processor.build_provider",
            lambda: None,
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
        assert session.scalar(
            select(DocumentSummary).where(
                DocumentSummary.document_version_id == version.id
            )
        ) is None
        session.refresh(version)
        assert version.processing_status == "ready"
        assert version.meta["ai_status"] == "not_configured"
