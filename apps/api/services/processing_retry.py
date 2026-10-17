"""Explicit recovery of failed base stages without restarting successful work."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.documents import Document, DocumentVersion
from ..models.processing import ProcessingJob


class ProcessingRetryError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


async def check_full_reprocess(db: AsyncSession, document: Document, version: DocumentVersion):
    """Protect existing stage work; repeated requests for the same run are idempotent."""
    if (document.meta or {}).get("external_source") == "database":
        raise ProcessingRetryError("数据库资料请从知识源刷新快照。", 400)
    active = await db.scalar(select(ProcessingJob).where(
        ProcessingJob.document_version_id == version.id,
        ProcessingJob.status.in_(["created", "retry", "processing"]),
    ).limit(1))
    if active is not None and not (active.stage == "parsing" and active.config_version == "2"):
        raise ProcessingRetryError("当前版本还有处理任务，请等待完成后再重新处理。")
    return active


async def retry_failed_stages(db: AsyncSession, document_id: int) -> dict:
    document = await db.scalar(
        select(Document).where(Document.id == document_id).with_for_update()
    )
    if document is None or document.is_deleted:
        raise ProcessingRetryError("资料不存在", 404)
    version = await db.get(DocumentVersion, document.current_version_id) if document.current_version_id else None
    if version is None:
        raise ProcessingRetryError("资料没有当前版本")
    # A worker may still be modifying this version. Never reset an executing job.
    running = await db.scalar(
        select(ProcessingJob.id).where(
            ProcessingJob.document_version_id == version.id,
            ProcessingJob.status == "processing",
        ).limit(1)
    )
    if running is not None:
        raise ProcessingRetryError("资料仍有任务执行中，请完成后再重试失败阶段")
    # Do not load/lock thousands of vector jobs just to recover a base stage.
    jobs = list((await db.scalars(
        select(ProcessingJob)
        .where(
            ProcessingJob.document_version_id == version.id,
            ProcessingJob.stage.in_(("stored", "parsing", "chunking", "understanding")),
        )
        .order_by(ProcessingJob.id.desc())
        .with_for_update()
    )).all())
    if any(job.status == "processing" for job in jobs):
        raise ProcessingRetryError("资料仍有任务执行中，请完成后再重试失败阶段")
    latest = {}
    for job in jobs:
        stage = "parsing" if job.stage == "stored" else job.stage
        latest.setdefault(stage, job)
    parsing = latest.get("parsing")
    if parsing is not None and parsing.status in {"created", "retry"}:
        return {"success": True, "message": "正文已在处理队列中", "job_ids": [], "stages": []}
    stages = [stage for stage in ("parsing", "chunking", "understanding")
              if stage in latest and latest[stage].status == "failed"]
    # Recover the failed prerequisite first; do not execute downstream work on
    # missing or stale parsed content. A later action can recover those stages.
    if "parsing" in stages:
        stages = ["parsing"]
    if not stages:
        return {"success": True, "message": "没有需要重试的基础处理阶段", "job_ids": [], "stages": []}
    if "parsing" in stages and latest["parsing"].config_version == "2":
        # A full reprocess regenerates AI organization and indexes, with costs
        # beyond a single-stage retry. Require its explicit confirmation again.
        raise ProcessingRetryError(
            "此解析任务来自完整重新处理，继续会重新生成整理信息；"
            "普通重试未执行，请到详情页确认“重新处理文档”后再继续"
        )
    if "parsing" not in stages and not version.structured_content:
        raise ProcessingRetryError("缺少解析内容，请到详情页检查并明确执行完整重新处理")
    for stage in stages:
        job = latest[stage]
        job.status = "created"
        job.retry_count = 0
        job.next_retry_at = None
        job.last_error = None
        job.error_details = None
        job.started_at = None
        job.finished_at = None
    if "parsing" in stages:
        version.processing_status = "created"
    else:
        # Parsed facts are still available. Stage failures (including any
        # unrelated vector failure) remain visible through their own jobs;
        # keeping a stale terminal version flag would prevent recovery even
        # after every resumed job succeeds.
        version.processing_status = "ready"
    # Preserve blob/hash, parsed facts, user organization, and the original job
    # strategy. Unlike full reprocessing, this does not request a URL refetch.
    await db.commit()
    labels = {"parsing": "解析", "chunking": "切片", "understanding": "整理"}
    return {
        "success": True,
        "message": "已重试失败阶段：" + "、".join(labels[stage] for stage in stages),
        "job_ids": [latest[stage].id for stage in stages],
        "stages": stages,
    }
