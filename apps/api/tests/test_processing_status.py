import asyncio

from sqlalchemy import select

from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.auth import AIRuntimeConfig
from apps.api.models.chunks import DocumentChunk
from apps.api.models.documents import DocumentVersion
from apps.api.models.embedding_profiles import ChunkEmbedding, EmbeddingProfile
from apps.api.models.processing import ProcessingJob
from apps.api.services.processing_status import (
    DocumentStatusError,
    compute_processing_status,
    load_pipeline_statuses,
    repair_active_vector,
)


def _session(dependency):
    return anext(dependency)


async def _seed_base(session, document_id, version_id):
    for stage in ("parsing", "chunking", "understanding"):
        session.add(
            ProcessingJob(
                document_id=document_id,
                document_version_id=version_id,
                stage=stage,
                status="completed",
                idempotency_key=f"status:{version_id}:{stage}",
                config_version="test",
            )
        )
    chunk = DocumentChunk(
        document_id=document_id,
        document_version_id=version_id,
        external_id="child-1",
        role="child",
        chunk_type="paragraph",
        order_index=0,
        content="用于检查向量处理状态。",
        search_text="状态 用于检查向量处理状态。",
        content_hash="c" * 64,
        char_count=12,
        token_estimate=6,
        is_current=True,
    )
    session.add(chunk)
    await session.flush()
    profile = EmbeddingProfile(
        provider="ollama",
        base_url="http://ollama:11434",
        model="bge-m3",
        dim=3,
        config_fingerprint="f" * 64,
        status="active",
    )
    session.add(profile)
    await session.flush()
    session.add(
        AIRuntimeConfig(
            singleton_key="singleton",
            provider="disabled",
            embedding_provider="ollama",
            embedding_base_url="http://ollama:11434",
            embedding_model="bge-m3",
            active_embedding_profile_id=profile.id,
        )
    )
    await session.commit()
    return chunk, profile


def test_compute_processing_status_reports_completed_vector_pipeline(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes",
        json={"title": "向量状态", "content": "用于检查向量处理状态。"},
    ).json()
    document_id = created["id"]
    version_id = created["current_version"]["id"]
    db_dependency = app.dependency_overrides[get_db]

    async def seed_completed():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            chunk, profile = await _seed_base(session, document_id, version_id)
            session.add(
                ChunkEmbedding(
                    profile_id=profile.id,
                    chunk_id=chunk.id,
                    content_hash=chunk.content_hash,
                    vector=[1.0, 0.0, 0.0],
                )
            )
            await session.commit()
        finally:
            await dependency.aclose()

    asyncio.run(seed_completed())

    async def run():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            return await compute_processing_status(session, document_id)
        finally:
            await dependency.aclose()

    body = asyncio.run(run())
    assert body["overall_status"] == "completed"
    assert body["keyword_searchable"] is True
    assert body["vector_searchable"] is True
    assert body["stages"]["chunking"]["child_chunks"] == 1
    assert body["stages"]["embedding"]["completed"] == 1
    assert body["stages"]["embedding"]["total"] == 1


def test_compute_processing_status_reports_failed_embedding(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes",
        json={"title": "失败向量", "content": "用于检查向量失败状态。"},
    ).json()
    document_id = created["id"]
    version_id = created["current_version"]["id"]
    db_dependency = app.dependency_overrides[get_db]

    async def seed_failed():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            chunk, profile = await _seed_base(session, document_id, version_id)
            session.add(
                ProcessingJob(
                    document_id=document_id,
                    document_version_id=version_id,
                    stage="embedding",
                    status="failed",
                    idempotency_key=f"status:{version_id}:embedding",
                    config_version="test",
                    embedding_profile_id=profile.id,
                    embedding_chunk_id=chunk.id,
                    last_error="provider timeout",
                )
            )
            await session.commit()
        finally:
            await dependency.aclose()

    asyncio.run(seed_failed())

    async def run():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            return await compute_processing_status(session, document_id)
        finally:
            await dependency.aclose()

    body = asyncio.run(run())
    assert body["overall_status"] == "failed"
    assert body["vector_searchable"] is False
    assert body["stages"]["embedding"]["status"] == "failed"
    assert body["stages"]["embedding"]["failed"] == 1


def test_compute_processing_status_does_not_mask_failed_version_with_running_stage(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes",
        json={"title": "失败版本", "content": "版本已失败但残留处理中任务。"},
    ).json()
    document_id = created["id"]
    version_id = created["current_version"]["id"]
    db_dependency = app.dependency_overrides[get_db]

    async def seed():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            await _seed_base(session, document_id, version_id)
            version = await session.get(DocumentVersion, version_id)
            assert version is not None
            version.processing_status = "failed"
            session.add(
                ProcessingJob(
                    document_id=document_id,
                    document_version_id=version_id,
                    stage="parsing",
                    status="processing",
                    idempotency_key=f"status:{version_id}:parsing:stale-running",
                    config_version="test",
                )
            )
            await session.commit()
        finally:
            await dependency.aclose()

    asyncio.run(seed())

    async def run():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            return await compute_processing_status(session, document_id)
        finally:
            await dependency.aclose()

    body = asyncio.run(run())
    assert body["overall_status"] == "failed"


def test_repair_active_vector_resets_only_failed_jobs_for_active_profile(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes",
        json={"title": "修复向量", "content": "用于检查定向修复。"},
    ).json()
    document_id = created["id"]
    version_id = created["current_version"]["id"]
    db_dependency = app.dependency_overrides[get_db]

    async def seed_failed_jobs():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            chunk, profile = await _seed_base(session, document_id, version_id)
            session.add(
                ProcessingJob(
                    document_id=document_id,
                    document_version_id=version_id,
                    stage="embedding",
                    status="failed",
                    idempotency_key=f"status:{version_id}:embedding:failed",
                    config_version="test",
                    embedding_profile_id=profile.id,
                    embedding_chunk_id=chunk.id,
                    last_error="provider timeout",
                )
            )
            await session.commit()
            return chunk, profile
        finally:
            await dependency.aclose()

    asyncio.run(seed_failed_jobs())

    async def repair():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            result = await repair_active_vector(session, document_id)
            jobs = (
                (
                    await session.execute(
                        select(ProcessingJob).where(
                            ProcessingJob.document_version_id == version_id,
                            ProcessingJob.stage == "embedding",
                        )
                    )
                )
                .scalars()
                .all()
            )
            return result, jobs
        finally:
            await dependency.aclose()

    result, jobs = asyncio.run(repair())
    assert result.reset == 1
    assert result.chunks_expected == 1
    statuses = {job.status for job in jobs}
    assert "failed" not in statuses
    assert "created" in statuses


def test_repair_active_vector_ignores_retired_profile_jobs(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes",
        json={"title": "旧向量", "content": "旧配置的失败任务不应被重建。"},
    ).json()
    document_id = created["id"]
    version_id = created["current_version"]["id"]
    db_dependency = app.dependency_overrides[get_db]

    async def seed_retired():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            chunk, _ = await _seed_base(session, document_id, version_id)
            retired = EmbeddingProfile(
                provider="ollama",
                base_url="http://ollama:11434",
                model="e5",
                dim=3,
                config_fingerprint="r" * 64,
                status="retired",
            )
            session.add(retired)
            await session.flush()
            session.add(
                ProcessingJob(
                    document_id=document_id,
                    document_version_id=version_id,
                    stage="embedding",
                    status="failed",
                    idempotency_key=f"status:{version_id}:embedding:retired",
                    config_version="test",
                    embedding_profile_id=retired.id,
                    embedding_chunk_id=chunk.id,
                    last_error="old config",
                )
            )
            await session.commit()
        finally:
            await dependency.aclose()

    asyncio.run(seed_retired())

    async def repair():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            return await repair_active_vector(session, document_id)
        finally:
            await dependency.aclose()

    result = asyncio.run(repair())
    assert result.enqueued == 1

    async def load_failed():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            job = (
                await session.execute(
                    select(ProcessingJob).where(
                        ProcessingJob.idempotency_key
                        == f"status:{version_id}:embedding:retired"
                    )
                )
            ).scalar_one()
            return job.status
        finally:
            await dependency.aclose()

    assert asyncio.run(load_failed()) == "failed"


def test_repair_active_vector_raises_when_no_active_profile(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes",
        json={"title": "无向量", "content": "没有配置向量索引。"},
    ).json()
    document_id = created["id"]
    db_dependency = app.dependency_overrides[get_db]

    async def repair():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            try:
                await repair_active_vector(session, document_id)
            except DocumentStatusError as exc:
                return str(exc)
            raise AssertionError("expected DocumentStatusError")
        finally:
            await dependency.aclose()

    assert asyncio.run(repair()) == "尚未启用向量模型，无法补建向量"


def test_compute_processing_status_infers_legacy_note_artifacts(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes",
        json={"title": "旧随手记", "content": "已有正文但没有历史解析任务。"},
    ).json()
    document_id = created["id"]
    version_id = created["current_version"]["id"]
    db_dependency = app.dependency_overrides[get_db]

    async def seed_artifacts():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            version = await session.get(DocumentVersion, version_id)
            version.processing_status = "ready"
            version.meta = {"ai_status": "not_configured"}
            session.add(
                DocumentChunk(
                    document_id=document_id,
                    document_version_id=version_id,
                    external_id="legacy-child",
                    role="child",
                    chunk_type="paragraph",
                    order_index=0,
                    content="已有正文但没有历史解析任务。",
                    search_text="旧随手记 已有正文但没有历史解析任务。",
                    content_hash="l" * 64,
                    char_count=14,
                    token_estimate=7,
                    is_current=True,
                )
            )
            await session.commit()
        finally:
            await dependency.aclose()

    asyncio.run(seed_artifacts())

    async def run():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            return await compute_processing_status(session, document_id)
        finally:
            await dependency.aclose()

    body = asyncio.run(run())
    assert body["stages"]["parsing"]["status"] == "completed"
    assert body["stages"]["chunking"]["status"] == "completed"
    assert body["stages"]["understanding"]["status"] == "skipped"
    assert "等待前一阶段" not in body["stages"]["parsing"]["message"]


def test_stale_embedding_hash_is_reported_missing_and_repair_is_idempotent(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes", json={"title": "旧向量", "content": "正文已经变化。"}
    ).json()
    document_id = created["id"]
    version_id = created["current_version"]["id"]
    db_dependency = app.dependency_overrides[get_db]

    async def seed_and_run():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            chunk, profile = await _seed_base(session, document_id, version_id)
            session.add(
                ChunkEmbedding(
                    profile_id=profile.id,
                    chunk_id=chunk.id,
                    content_hash="old-hash",
                    vector=[1.0, 0.0, 0.0],
                )
            )
            await session.commit()
            before = await compute_processing_status(session, document_id)
            first = await repair_active_vector(session, document_id)
            second = await repair_active_vector(session, document_id)
            return before, first, second
        finally:
            await dependency.aclose()

    before, first, second = asyncio.run(seed_and_run())
    assert before["vector_searchable"] is False
    assert before["stages"]["embedding"]["missing"] == 1
    assert first.enqueued == 1
    assert second.enqueued == 0
    assert second.skipped == 1


def test_sampled_dataset_uses_selected_chunks_as_vector_denominator(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes", json={"title": "抽样数据", "content": "代表性数据行。"}
    ).json()
    document_id = created["id"]
    version_id = created["current_version"]["id"]
    db_dependency = app.dependency_overrides[get_db]

    async def seed_and_run():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            version = await session.get(DocumentVersion, version_id)
            version.processing_status = "ready"
            version.meta = {
                "ai_status": "not_configured",
                "embedding_strategy": {"mode": "sampled", "selected_chunks": 2},
            }
            profile = EmbeddingProfile(
                provider="ollama",
                base_url="http://ollama:11434",
                model="bge-m3",
                dim=3,
                config_fingerprint="s" * 64,
                status="active",
            )
            session.add(profile)
            await session.flush()
            session.add(
                AIRuntimeConfig(
                    singleton_key="singleton",
                    provider="disabled",
                    embedding_provider="ollama",
                    embedding_base_url="http://ollama:11434",
                    embedding_model="bge-m3",
                    active_embedding_profile_id=profile.id,
                )
            )
            chunks = []
            for index in range(5):
                chunk = DocumentChunk(
                    document_id=document_id,
                    document_version_id=version_id,
                    external_id=f"row-{index}",
                    role="child",
                    chunk_type="table_row",
                    order_index=index,
                    content=f"row {index}",
                    search_text=f"row {index}",
                    content_hash=str(index) * 64,
                    char_count=5,
                    token_estimate=2,
                    is_current=True,
                )
                session.add(chunk)
                chunks.append(chunk)
            await session.flush()
            for chunk in (chunks[0], chunks[-1]):
                session.add(
                    ChunkEmbedding(
                        profile_id=profile.id,
                        chunk_id=chunk.id,
                        content_hash=chunk.content_hash,
                        vector=[1.0, 0.0, 0.0],
                    )
                )
            await session.commit()
            return (await load_pipeline_statuses(session, [version_id]))[version_id]
        finally:
            await dependency.aclose()

    status = asyncio.run(seed_and_run())
    assert status["stages"]["chunking"]["child_chunks"] == 5
    assert status["stages"]["embedding"]["total"] == 2
    assert status["stages"]["embedding"]["completed"] == 2
    assert status["vector_searchable"] is True


def test_overview_includes_pipeline_only_when_requested(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes", json={"title": "状态总览", "content": "一次读取处理状态。"}
    ).json()
    document_id = created["id"]
    plain = test_client.get("/api/documents/overview?limit=10").json()
    enriched = test_client.get(
        "/api/documents/overview?limit=10&include_processing=true"
    ).json()
    assert next(row for row in plain if row["id"] == document_id)["pipeline"] is None
    assert next(row for row in enriched if row["id"] == document_id)["pipeline"]


def test_governance_spreadsheet_skips_chunk_and_vector_stages(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes", json={"title": "待治理表格", "content": "原始事实仍保留。"}
    ).json()
    document_id = created["id"]
    version_id = created["current_version"]["id"]
    db_dependency = app.dependency_overrides[get_db]

    async def seed_governance_status():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            version = await session.get(DocumentVersion, version_id)
            version.meta = {
                **(version.meta or {}),
                "spreadsheet_processing": {
                    "mode": "governance",
                    "governance_required": True,
                },
            }
            jobs = list(
                (
                    await session.execute(
                        select(ProcessingJob).where(
                            ProcessingJob.document_version_id == version_id
                        )
                    )
                ).scalars()
            )
            chunking = next((job for job in jobs if job.stage == "chunking"), None)
            if chunking is None:
                session.add(
                    ProcessingJob(
                        document_id=document_id,
                        document_version_id=version_id,
                        stage="chunking",
                        status="completed",
                        idempotency_key=f"governance:{version_id}:chunking",
                        config_version="test",
                    )
                )
            else:
                chunking.status = "completed"
            await session.commit()
            return (await load_pipeline_statuses(session, [version_id]))[version_id]
        finally:
            await dependency.aclose()

    status = asyncio.run(seed_governance_status())

    assert status["keyword_searchable"] is False
    assert status["vector_searchable"] is False
    assert status["stages"]["chunking"]["status"] == "skipped"
    assert status["stages"]["chunking"]["child_chunks"] == 0
    assert status["stages"]["embedding"]["status"] == "skipped"
    assert status["stages"]["embedding"]["total"] == 0


def test_overview_exposes_total_for_pagination(client):
    test_client, _ = client
    for index in range(3):
        test_client.post(
            "/api/notes",
            json={"title": f"分页资料 {index}", "content": f"正文 {index}"},
        )

    first = test_client.get("/api/documents/overview?limit=2&offset=0")
    second = test_client.get("/api/documents/overview?limit=2&offset=2")
    assert first.status_code == 200
    assert first.headers["X-Total-Count"] == "3"
    assert len(first.json()) == 2
    assert second.headers["X-Total-Count"] == "3"
    assert len(second.json()) == 1


def test_vector_repair_endpoint_reports_missing_active_profile(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes", json={"title": "未配置向量", "content": "仍可关键词检索。"}
    ).json()
    response = test_client.post(
        "/api/documents/batch/repair-vectors",
        json={"document_ids": [created["id"]]},
    )
    assert response.status_code == 400
    assert "尚未启用向量模型" in response.json()["detail"]


def test_pdf_extraction_summary_is_propagated_to_pipeline(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes",
        json={"title": "扫描 PDF 摘要", "content": "用于检查 OCR 摘要。"},
    )
    document_id = created.json()["id"]
    version_id = created.json()["current_version"]["id"]
    db_dependency = app.dependency_overrides[get_db]

    pdf_extraction = {
        "version": "pdf-hybrid-v1",
        "engine": "tesseract",
        "page_count": 12,
        "native_text_pages": [1, 2, 3],
        "image_pages": [4, 5, 6, 7, 8, 9, 10, 11, 12],
        "ocr_candidate_pages": [4, 5, 6, 7, 8, 9, 10, 11, 12],
        "ocr_completed_pages": [4, 5, 6, 7, 8, 9],
        "ocr_failed_pages": [10],
        "ocr_skipped_pages": [11, 12],
        "ocr_status": "partial",
    }

    async def seed_extraction():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            version = await session.get(DocumentVersion, version_id)
            version.processing_status = "ready"
            version.meta = {"ai_status": "not_configured"}
            version.structured_content = {
                "schema_version": 1,
                "document_type": "pdf",
                "blocks": [],
                "metadata": {
                    "page_count": 12,
                    "pdf_extraction": pdf_extraction,
                },
            }
            session.add(version)
            await session.commit()
        finally:
            await dependency.aclose()

    asyncio.run(seed_extraction())

    async def run():
        dependency = db_dependency()
        session = await _session(dependency)
        try:
            return await compute_processing_status(session, document_id)
        finally:
            await dependency.aclose()

    body = asyncio.run(run())
    extraction = body["stages"]["parsing"]["extraction"]
    assert extraction["version"] == "pdf-hybrid-v1"
    assert extraction["engine"] == "tesseract"
    assert extraction["page_count"] == 12
    assert extraction["native_text_pages"] == 3
    assert extraction["ocr_candidate_pages"] == 9
    assert extraction["ocr_completed_pages"] == 6
    assert extraction["ocr_failed_pages"] == [10]
    assert extraction["ocr_skipped_pages"] == [11, 12]
    assert extraction["ocr_status"] == "partial"
    assert body["overall_status"] != "failed"
