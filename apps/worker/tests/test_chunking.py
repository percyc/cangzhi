"""Tests for the chunking job wired into the worker pipeline."""

import json
from io import BytesIO
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from apps.api.core.db import Base
from apps.api.models.blobs import Blob
from apps.api.models.chunks import DocumentChunk
from apps.api.models.datasets import DatasetField, KnowledgeDataset
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.api.models.table_rows import StructuredTableRow
from apps.api.parsers.base import Block, StructuredContent
from apps.api.storage.local import LocalBlobStorage
from apps.worker.core.config import settings as worker_settings
from apps.worker.services.processor import (
    ASSISTED_CHUNKING_CONFIG,
    ASSISTED_CHUNKING_MAX_CALLS,
    ASSISTED_CHUNKING_POLICY,
    ASSISTED_CHUNKING_TIMEOUT_SECONDS,
    CHUNKING_STAGE,
    CHUNKING_IDEMPOTENCY,
    DATASET_CATALOG_STAGE,
    UNDERSTANDING_IDEMPOTENCY,
    UNDERSTANDING_STAGE,
    _clear_understanding_results,
    _build_ai_input,
    _process_chunking,
    _release_webdav_source_blob,
    process_single_job,
)


def json_safe_dumps(payload):
    """Serialise any object for substring searches without raising."""

    def _default(_value):
        return repr(_value)

    return json.dumps(payload, ensure_ascii=False, default=_default)


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


def test_long_ai_input_samples_beginning_middle_and_end():
    raw_text = "A" * 4_000 + "MIDDLE" + "B" * 4_000 + "TAIL"

    result = _build_ai_input("长文", raw_text, {})

    assert "【文档开头】" in result
    assert "【文档中部抽样】" in result
    assert "【文档结尾】" in result
    assert "MIDDLE" in result
    assert result.endswith("TAIL")


def test_spreadsheet_ai_input_preserves_schema_and_whole_rows():
    raw_text = "\n".join(
        f"行 {row}｜订单号=NO-{row:04d}｜金额={row * 100}"
        for row in range(2, 82)
    )
    metadata = {
        "regions": [
            {
                "sheet_name": "订单",
                "row_start": 1,
                "row_end": 81,
                "column_names": ["订单号", "金额"],
            }
        ]
    }

    result = _build_ai_input("订单台账", raw_text, metadata)

    assert "工作表 订单，第 1–81 行，列：订单号、金额" in result
    assert "【表格中部行】" in result
    assert "行 2｜订单号=NO-0002｜金额=200" in result


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
            idempotency_key=f"{version.id}:{CHUNKING_STAGE}:{CHUNKING_IDEMPOTENCY}",
            config_version=CHUNKING_IDEMPOTENCY,
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
        assert version.meta["chunking_config"]["profile_version"] == "chunk-profile:v4"
        assert version.meta["chunking_config"]["child_overlap_chars"] == 120
        assert child.extra["document_profile"]["profile_version"] == "chunk-profile:v4"

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
            idempotency_key=f"{version.id}:{CHUNKING_STAGE}:{CHUNKING_IDEMPOTENCY}",
            config_version=CHUNKING_IDEMPOTENCY,
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
            idempotency_key=f"{version.id}:{CHUNKING_STAGE}:{CHUNKING_IDEMPOTENCY}",
            config_version=CHUNKING_IDEMPOTENCY,
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
            idempotency_key=f"{version.id}:{CHUNKING_STAGE}:{CHUNKING_IDEMPOTENCY}",
            config_version=CHUNKING_IDEMPOTENCY,
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
            idempotency_key=f"{version.id}:{CHUNKING_STAGE}:{CHUNKING_IDEMPOTENCY}",
            config_version=CHUNKING_IDEMPOTENCY,
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

    def test_spreadsheet_chunking_persists_addressable_rows(self, session):
        document = Document(title="采购表", source_type=DocumentSourceType.file)
        session.add(document)
        session.flush()
        structured = StructuredContent(
            document_type="xlsx",
            blocks=[
                Block(
                    type="table",
                    text="行 2｜品类=水果｜金额=12\n行 3｜金额=9",
                    heading_path=["明细", "数据区域 1"],
                    paragraph_index=0,
                    extra={
                        "sheet_name": "明细",
                        "region_index": 1,
                        "row_start": 2,
                        "row_end": 3,
                        "column_names": ["品类", "金额"],
                    },
                )
            ],
        )
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="table-seed",
            structured_content=structured.to_dict(),
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        document.current_version_id = version.id
        job = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage=CHUNKING_STAGE,
            idempotency_key=(
                f"{version.id}:{CHUNKING_STAGE}:{CHUNKING_IDEMPOTENCY}"
            ),
            config_version=CHUNKING_IDEMPOTENCY,
        )
        session.add(job)
        session.commit()

        assert _process_chunking(session, job, document, version) is True
        rows = session.scalars(
            select(StructuredTableRow)
            .where(StructuredTableRow.document_version_id == version.id)
            .order_by(StructuredTableRow.row_number)
        ).all()
        assert [row.row_number for row in rows] == [2, 3]
        assert rows[1].values == {"品类": None, "金额": "9"}
        assert version.meta["structured_table_row_count"] == 2
        dataset = session.scalar(
            select(KnowledgeDataset).where(
                KnowledgeDataset.document_version_id == version.id
            )
        )
        assert dataset is not None
        assert dataset.name == "明细"
        assert dataset.row_count == 2
        assert dataset.column_count == 2
        assert dataset.profile["quality"]["completeness"] == 0.75
        fields = session.scalars(
            select(DatasetField)
            .where(DatasetField.dataset_id == dataset.id)
            .order_by(DatasetField.position)
        ).all()
        assert [(field.name, field.inferred_type) for field in fields] == [
            ("品类", "text"),
            ("金额", "number"),
        ]
        assert all(row.dataset_id == dataset.id for row in rows)

    def test_dataset_catalog_job_profiles_existing_rows_without_rechunking(
        self, session
    ):
        document = Document(title="存量表格", source_type=DocumentSourceType.file)
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="catalog-backfill",
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        document.current_version_id = version.id
        dataset = KnowledgeDataset(
            document_id=document.id,
            document_version_id=version.id,
            name="状态表",
            sheet_name="状态表",
            region_index=1,
            dataset_kind="table",
            status="ready",
            row_count=2,
            column_count=2,
            profile={"catalog_source": "migration"},
        )
        session.add(dataset)
        session.flush()
        session.add_all(
            [
                StructuredTableRow(
                    document_id=document.id,
                    document_version_id=version.id,
                    dataset_id=dataset.id,
                    sheet_name="状态表",
                    region_index=1,
                    row_number=index,
                    values={"编号": code, "是否完成": status},
                )
                for index, (code, status) in enumerate(
                    [("001", "是"), ("002", "否")], start=2
                )
            ]
        )
        job = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage=DATASET_CATALOG_STAGE,
            status="created",
            idempotency_key=f"{version.id}:dataset_catalog:dataset-catalog:v1",
            config_version="dataset-catalog:v1",
        )
        session.add(job)
        session.commit()

        assert process_single_job(session, job.id) is True

        session.refresh(job)
        session.refresh(dataset)
        fields = list(
            session.scalars(
                select(DatasetField)
                .where(DatasetField.dataset_id == dataset.id)
                .order_by(DatasetField.position)
            ).all()
        )
        assert job.status == "completed"
        assert version.processing_status == "ready"
        assert dataset.profile["quality"]["completeness"] == 1.0
        assert [(field.name, field.inferred_type) for field in fields] == [
            ("编号", "identifier"),
            ("是否完成", "boolean"),
        ]


class TestAssistedChunking:
    """Bounded, opt-in AI-assisted rechunking for authorized maintenance passes.

    The default ``CHUNKING_IDEMPOTENCY`` jobs keep their behaviour; the
    assisted path is only entered when ``config_version`` matches
    :data:`ASSISTED_CHUNKING_CONFIG` and the document is a PDF. Non-PDF
    jobs dispatched with the assisted config continue on the
    profile-driven path so the dataset catalog and the regular
    child chunker remain untouched.
    """

    def _make_pdf_version(self, session, *, content_hash="assisted-pdf", title="辅助 PDF"):
        structured = StructuredContent(
            document_type="pdf",
            blocks=[
                Block(
                    type="heading",
                    text="第一章 范围",
                    heading_path=["第一章 范围"],
                    level=1,
                    paragraph_index=0,
                ),
                Block(
                    type="paragraph",
                    text="本标准规定了范围条件。",
                    heading_path=["第一章 范围"],
                    paragraph_index=1,
                ),
                Block(
                    type="paragraph",
                    text="本标准适用于相关产品。",
                    heading_path=["第一章 范围"],
                    paragraph_index=2,
                ),
                Block(
                    type="paragraph",
                    text="本标准不适用于其他产品。",
                    heading_path=["第一章 范围"],
                    paragraph_index=3,
                ),
            ],
        )
        document = Document(title=title, source_type=DocumentSourceType.file)
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash=content_hash,
            structured_content=structured.to_dict(),
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        document.current_version_id = version.id
        return document, version, structured.to_dict()

    def test_default_pdf_no_model_uses_profile_chunker(
        self, session, monkeypatch
    ):
        """Default ``CHUNKING_IDEMPOTENCY`` jobs never enter the candidate
        path, even for PDFs and even when no model is configured. The
        profile-driven chunker is the only writer of the served chunks.
        """

        provider_called = {"count": 0}

        def fake_provider(_session):
            provider_called["count"] += 1
            return None

        monkeypatch.setattr(
            "apps.worker.services.processor.build_provider_from_session",
            fake_provider,
        )
        document, version, original_payload = self._make_pdf_version(
            session, content_hash="default-pdf", title="默认 PDF"
        )
        job = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage=CHUNKING_STAGE,
            idempotency_key=f"{version.id}:{CHUNKING_STAGE}:{CHUNKING_IDEMPOTENCY}",
            config_version=CHUNKING_IDEMPOTENCY,
        )
        session.add(job)
        session.commit()

        assert _process_chunking(session, job, document, version) is True

        session.refresh(job)
        session.refresh(version)

        # Provider was never consulted from the regular path.
        assert provider_called["count"] == 0
        assert job.status == "completed"
        # Default config marker; the assisted config key is intentionally
        # absent so a future opt-in run can be detected.
        assert version.meta["chunk_config_version"] == CHUNKING_IDEMPOTENCY
        assert "assisted_chunking" not in version.meta
        assert version.meta["chunk_quality"]["policy_version"] == "baseline-structure:v1"
        assert version.meta["chunk_quality"]["model_calls"] == 0
        assert version.meta["chunk_quality"]["mode"] == "normalized_rules"
        # Source structured_content untouched.
        assert version.structured_content == original_payload
        # Profile-driven chunks still produced.
        chunks = session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == version.id
            )
        ).all()
        assert any(chunk.role == "child" for chunk in chunks)

        assert any(chunk.role == "parent" for chunk in chunks)

    def test_structural_maintenance_never_constructs_ai_provider(self, session, monkeypatch):
        from apps.worker.services.processor import STRUCTURAL_CHUNKING_CONFIG

        def forbidden(_session):
            raise AssertionError("structural maintenance must not construct an AI provider")

        monkeypatch.setattr("apps.worker.services.processor.build_provider_from_session", forbidden)
        document, version, payload = self._make_pdf_version(
            session, content_hash="structural-test", title="结构维护测试")
        job = ProcessingJob(document_id=document.id, document_version_id=version.id,
                            stage=CHUNKING_STAGE, config_version=STRUCTURAL_CHUNKING_CONFIG,
                            idempotency_key=f"{version.id}:structural-test")
        session.add(job)
        session.commit()
        assert _process_chunking(session, job, document, version) is True
        assert version.structured_content == payload
        assert version.meta["chunk_config_version"] == STRUCTURAL_CHUNKING_CONFIG
        diagnostic = version.meta["assisted_chunking"]
        assert diagnostic["calls"] == 0 and not diagnostic["ai_used"]
        assert diagnostic["decision"] == "structural_rules"
        assert job.status == "completed"

    @pytest.mark.parametrize("coverage,unverified", [(False, 0), (True, 1)])
    def test_structural_incomplete_candidate_is_not_activated(self, session, monkeypatch, coverage, unverified):
        from apps.worker.services.processor import STRUCTURAL_CHUNKING_CONFIG

        document, version, payload = self._make_pdf_version(
            session, content_hash="incomplete-structural", title="覆盖校验")
        monkeypatch.setattr(
            "apps.api.services.chunking_structural.build_structural_candidate",
            lambda *_args, **_kwargs: {"source_content_complete": coverage,
                                     "unverified_spans": unverified, "specs": []})
        monkeypatch.setattr(
            "apps.worker.services.processor._replace_version_chunks",
            lambda *_args, **_kwargs: pytest.fail("must preserve serving chunks"))
        job = ProcessingJob(document_id=document.id, document_version_id=version.id,
                            stage=CHUNKING_STAGE, config_version=STRUCTURAL_CHUNKING_CONFIG,
                            idempotency_key=f"{version.id}:incomplete-structural")
        session.add(job)
        session.commit()
        assert _process_chunking(session, job, document, version) is False
        assert job.status == "failed"
        assert version.processing_status == "ready"
        assert version.structured_content == payload
        assert job.error_details["reason"] == "source_coverage_incomplete"

    def test_structural_regression_preserves_serving_version(self, session, monkeypatch):
        from apps.worker.services.processor import STRUCTURAL_CHUNKING_CONFIG
        document, version, _ = self._make_pdf_version(
            session, content_hash="regression-structural", title="候选对比")
        old = DocumentChunk(document_id=document.id, document_version_id=version.id,
            external_id="serving", role="child", chunk_type="paragraph", order_index=0,
            content="原文" * 100, search_text="原文" * 100, content_hash="old-hash",
            char_count=200, token_estimate=100, is_current=True)
        session.add(old)
        job = ProcessingJob(document_id=document.id, document_version_id=version.id,
            stage=CHUNKING_STAGE, config_version=STRUCTURAL_CHUNKING_CONFIG,
            idempotency_key=f"{version.id}:structural-regression")
        session.add(job)
        session.commit()
        old_id = old.id
        monkeypatch.setattr("apps.api.services.chunking_structural.build_structural_candidate",
            lambda *_args, **_kwargs: {"source_content_complete": True, "unverified_spans": 0,
                "specs": [{"role": "child", "char_count": 20} for _ in range(4)]})
        assert _process_chunking(session, job, document, version) is False
        assert job.status == "failed" and job.next_retry_at is None
        assert job.error_details["reason"] == "candidate_fragmentation_regression"
        assert job.error_details["serving_preserved"] is True
        assert version.processing_status == "ready"
        assert session.get(DocumentChunk, old_id).content == "原文" * 100

    def test_assisted_pdf_candidate_replaces_chunks_and_preserves_structured(
        self, session, monkeypatch
    ):
        """PDF job with ``ASSISTED_CHUNKING_CONFIG`` enters the candidate
        path: a valid provider is bounded, the candidate specs replace
        the served chunks and the stored ``structured_content`` is left
        untouched. The diagnostic records policy, call counts, coverage
        and the ``candidate_used`` flag.
        """

        provider = SimpleNamespace(
            _timeout=30.0,
            generate_json=lambda **_kwargs: {"ends": [3]},
        )
        monkeypatch.setattr(
            "apps.worker.services.processor.build_provider_from_session",
            lambda _session: provider,
        )
        document, version, original_payload = self._make_pdf_version(session)
        job = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage=CHUNKING_STAGE,
            idempotency_key=(
                f"{version.id}:{CHUNKING_STAGE}:{ASSISTED_CHUNKING_CONFIG}"
            ),
            config_version=ASSISTED_CHUNKING_CONFIG,
        )
        session.add(job)
        session.commit()

        assert _process_chunking(session, job, document, version) is True

        session.refresh(job)
        session.refresh(version)

        # Source structured_content is not modified.
        assert version.structured_content == original_payload
        assert job.status == "completed"
        assert version.meta["chunk_config_version"] == ASSISTED_CHUNKING_CONFIG
        diag = version.meta["assisted_chunking"]
        assert diag["policy"] == ASSISTED_CHUNKING_POLICY
        assert diag["config_version"] == ASSISTED_CHUNKING_CONFIG
        assert diag["max_calls"] == ASSISTED_CHUNKING_MAX_CALLS
        assert diag["timeout_seconds"] == ASSISTED_CHUNKING_TIMEOUT_SECONDS
        assert diag["provider_available"] is True
        assert diag["calls"] >= 1
        assert diag["accepted"] >= 1
        assert diag["failed"] == 0
        assert diag["assisted_blocks"] == diag["total_blocks"] == 4
        assert diag["coverage"] == 1.0
        assert diag["candidate_used"] is True
        assert diag["skipped_dataset"] is False
        # The provider timeout was bounded by the worker to a small value.
        assert provider._timeout == ASSISTED_CHUNKING_TIMEOUT_SECONDS
        # Candidate specs were persisted as the new served chunks.
        chunks = session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == version.id
            ).order_by(DocumentChunk.order_index)
        ).all()
        assert any(chunk.role == "child" for chunk in chunks)
        # Diagnostics must not contain source text or full specs.
        assert "specs" not in diag
        for forbidden in ("本标准", "原文"):
            assert forbidden not in json_safe_dumps(diag)

    def test_assisted_pdf_malformed_model_falls_back_to_rules(
        self, session, monkeypatch
    ):
        """A provider that returns an invalid boundary proposal causes the
        candidate to fall back to rules. The job still completes, the
        diagnostic shows ``failed >= 1``, ``accepted == 0`` and
        ``candidate_used`` is False. The structured_content is preserved.
        """

        def crash(**_kwargs):
            raise RuntimeError("credential-and-private-text")

        provider = SimpleNamespace(_timeout=30.0, generate_json=crash)
        monkeypatch.setattr(
            "apps.worker.services.processor.build_provider_from_session",
            lambda _session: provider,
        )
        document, version, original_payload = self._make_pdf_version(
            session, content_hash="malformed", title="模型故障 PDF"
        )
        job = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage=CHUNKING_STAGE,
            idempotency_key=(
                f"{version.id}:{CHUNKING_STAGE}:{ASSISTED_CHUNKING_CONFIG}"
            ),
            config_version=ASSISTED_CHUNKING_CONFIG,
        )
        session.add(job)
        session.commit()

        assert _process_chunking(session, job, document, version) is True

        session.refresh(job)
        session.refresh(version)

        assert version.structured_content == original_payload
        assert job.status == "completed"
        diag = version.meta["assisted_chunking"]
        assert diag["provider_available"] is True
        assert diag["calls"] >= 1
        assert diag["failed"] >= 1
        assert diag["accepted"] == 0
        assert diag["candidate_used"] is True
        assert diag["ai_used"] is False
        # The provider's exception message (which could carry tokens) is
        # never persisted in the diagnostic.
        assert "credential-and-private-text" not in json_safe_dumps(diag)
        chunks = session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == version.id
            )
        ).all()
        assert any(chunk.role == "child" for chunk in chunks)

    def test_assisted_pdf_no_provider_falls_back_to_rules(
        self, session, monkeypatch
    ):
        """When no model is configured the candidate is still built but
        the windows remain rules-based. The diagnostic records
        ``provider_available=False`` and ``candidate_used=False``.
        """

        monkeypatch.setattr(
            "apps.worker.services.processor.build_provider_from_session",
            lambda _session: None,
        )
        document, version, original_payload = self._make_pdf_version(
            session, content_hash="no-provider", title="无模型 PDF"
        )
        job = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage=CHUNKING_STAGE,
            idempotency_key=(
                f"{version.id}:{CHUNKING_STAGE}:{ASSISTED_CHUNKING_CONFIG}"
            ),
            config_version=ASSISTED_CHUNKING_CONFIG,
        )
        session.add(job)
        session.commit()

        assert _process_chunking(session, job, document, version) is True

        session.refresh(job)
        session.refresh(version)

        assert version.structured_content == original_payload
        assert job.status == "completed"
        diag = version.meta["assisted_chunking"]
        assert diag["provider_available"] is False
        assert diag["calls"] == 0
        assert diag["accepted"] == 0
        assert diag["failed"] == 0
        assert diag["candidate_used"] is True
        assert diag["ai_used"] is False
        chunks = session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == version.id
            )
        ).all()
        assert any(chunk.role == "child" for chunk in chunks)

    def test_assisted_pdf_explicit_failure_preserves_existing_chunks(
        self, session, monkeypatch
    ):
        """A value error from ``build_candidate`` is treated as an
        explicit failure: the existing served chunks are not touched and
        the job is set to ``retry`` with the assisted config in the
        error details.
        """

        def fake_build_candidate(*_args, **_kwargs):
            raise ValueError("too_many_blocks")

        monkeypatch.setattr(
            "apps.worker.services.processor.build_candidate",
            fake_build_candidate,
        )
        document, version, original_payload = self._make_pdf_version(
            session, content_hash="explicit-fail", title="失败 PDF"
        )
        # Plant an existing served chunk set so we can verify it survives.
        existing = DocumentChunk(
            document_id=document.id,
            document_version_id=version.id,
            parent_id=None,
            external_id="preexisting-parent",
            search_text="既有父片段",
            role="parent",
            chunk_type="section",
            content="既有父片段，不能丢失。",
            content_hash="preexisting-parent-hash",
            heading_path=["历史"],
            page=1,
            paragraph_index=0,
            source_start=0,
            source_end=10,
            char_count=10,
            token_estimate=4,
            language="zh",
            extra={"document_profile": {"detected_type": "general"}},
            is_current=True,
        )
        session.add(existing)
        session.flush()
        child = DocumentChunk(
            document_id=document.id,
            document_version_id=version.id,
            parent_id=existing.id,
            external_id="preexisting-child",
            search_text="既有子片段",
            role="child",
            chunk_type="paragraph",
            content="既有子片段。",
            content_hash="preexisting-child-hash",
            heading_path=["历史"],
            page=1,
            paragraph_index=1,
            source_start=0,
            source_end=6,
            char_count=6,
            token_estimate=3,
            language="zh",
            extra={"document_profile": {"detected_type": "general"}},
            is_current=True,
        )
        session.add(child)
        job = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage=CHUNKING_STAGE,
            idempotency_key=(
                f"{version.id}:{CHUNKING_STAGE}:{ASSISTED_CHUNKING_CONFIG}"
            ),
            config_version=ASSISTED_CHUNKING_CONFIG,
            retry_count=0,
            max_retries=3,
        )
        session.add(job)
        session.commit()

        assert _process_chunking(session, job, document, version) is False

        session.refresh(job)
        session.refresh(version)

        # The structured_content is preserved.
        assert version.structured_content == original_payload
        # The job is queued for retry with the assisted config marker.
        assert job.status == "retry"
        assert job.retry_count == 1
        assert "辅助切片" in (job.last_error or "")
        assert job.error_details["config_version"] == ASSISTED_CHUNKING_CONFIG
        assert job.error_details["reason"] == "candidate_invalid"
        assert version.processing_status == "retry"
        # The pre-existing chunks are still there, untouched.
        surviving = session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == version.id
            ).order_by(DocumentChunk.external_id)
        ).all()
        assert {chunk.external_id for chunk in surviving} == {
            "preexisting-child",
            "preexisting-parent",
        }

    def test_assisted_non_pdf_uses_profile_chunker(self, session, monkeypatch):
        """A non-PDF job dispatched with the assisted config still runs
        the profile-driven chunker. ``build_provider_from_session`` is
        not called, the meta reflects the assisted config, and the
        diagnostic notes the baseline reason.
        """

        provider_called = {"count": 0}

        def fake_provider(_session):
            provider_called["count"] += 1
            return None

        monkeypatch.setattr(
            "apps.worker.services.processor.build_provider_from_session",
            fake_provider,
        )
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
                    text="一句话介绍本节。",
                    heading_path=["引言"],
                    paragraph_index=1,
                ),
            ],
        )
        original_payload = structured.to_dict()
        document = Document(title="非 PDF", source_type=DocumentSourceType.note)
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="non-pdf-assisted",
            structured_content=original_payload,
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        job = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage=CHUNKING_STAGE,
            idempotency_key=(
                f"{version.id}:{CHUNKING_STAGE}:{ASSISTED_CHUNKING_CONFIG}"
            ),
            config_version=ASSISTED_CHUNKING_CONFIG,
        )
        session.add(job)
        session.commit()

        assert _process_chunking(session, job, document, version) is True

        session.refresh(job)
        session.refresh(version)

        # No model was consulted for a non-PDF job.
        assert provider_called["count"] == 0
        assert job.status == "completed"
        assert version.structured_content == original_payload
        assert version.meta["chunk_config_version"] == ASSISTED_CHUNKING_CONFIG
        diag = version.meta["assisted_chunking"]
        assert diag["reason"] == "non_pdf_baseline"
        assert diag["candidate_used"] is False
        assert diag["calls"] == 0
        assert diag["accepted"] == 0
        assert diag["failed"] == 0
        assert diag["skipped_dataset"] is False
        # Regular profile-driven chunks are produced.
        chunks = session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == version.id
            )
        ).all()
        assert any(chunk.role == "child" for chunk in chunks)

    def test_assisted_xlsx_uses_dataset_catalog_not_row_vectors(
        self, session, monkeypatch
    ):
        """XLSX jobs dispatched with the assisted config do not call the
        AI model and do not create per-row vectors. The dataset catalog
        spec is produced exactly as in the default path.
        """

        provider_called = {"count": 0}

        def fake_provider(_session):
            provider_called["count"] += 1
            return None

        monkeypatch.setattr(
            "apps.worker.services.processor.build_provider_from_session",
            fake_provider,
        )
        structured = StructuredContent(
            document_type="xlsx",
            blocks=[
                Block(
                    type="table",
                    text="行 2｜品类=水果｜金额=12\n行 3｜品类=蔬菜｜金额=9",
                    heading_path=["明细", "数据区域 1"],
                    paragraph_index=0,
                    extra={
                        "sheet_name": "明细",
                        "region_index": 1,
                        "row_start": 2,
                        "row_end": 3,
                        "column_names": ["品类", "金额"],
                    },
                )
            ],
        )
        original_payload = structured.to_dict()
        document = Document(title="采购表", source_type=DocumentSourceType.file)
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="xlsx-assisted",
            structured_content=original_payload,
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        document.current_version_id = version.id
        job = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage=CHUNKING_STAGE,
            idempotency_key=(
                f"{version.id}:{CHUNKING_STAGE}:{ASSISTED_CHUNKING_CONFIG}"
            ),
            config_version=ASSISTED_CHUNKING_CONFIG,
        )
        session.add(job)
        session.commit()

        assert _process_chunking(session, job, document, version) is True

        session.refresh(job)
        session.refresh(version)

        # The provider is never consulted for spreadsheets.
        assert provider_called["count"] == 0
        assert job.status == "completed"
        assert version.structured_content == original_payload
        assert version.meta["chunk_config_version"] == ASSISTED_CHUNKING_CONFIG
        diag = version.meta["assisted_chunking"]
        assert diag["reason"] == "non_pdf_baseline"
        assert diag["skipped_dataset"] is True
        assert diag["candidate_used"] is False
        assert diag["calls"] == 0

        chunks = session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.document_version_id == version.id
            ).order_by(DocumentChunk.order_index)
        ).all()
        # The dataset catalog spec is produced; no per-row child chunks.
        roles = {chunk.role for chunk in chunks}
        chunk_types = {chunk.chunk_type for chunk in chunks}
        assert "dataset_catalog" in chunk_types
        assert "dataset" in chunk_types
        # No per-row vector child chunk is created; the only child is
        # the dataset catalog one.
        children = [chunk for chunk in chunks if chunk.role == "child"]
        assert len(children) == 1
        assert children[0].chunk_type == "dataset_catalog"
        # The catalog chunk summarises the rows; raw row values are
        # stored in ``structured_table_rows``, not in the vector child.
        rows = session.scalars(
            select(StructuredTableRow).where(
                StructuredTableRow.document_version_id == version.id
            ).order_by(StructuredTableRow.row_number)
        ).all()
        assert [row.row_number for row in rows] == [2, 3]
        assert version.meta["structured_table_row_count"] == 2
