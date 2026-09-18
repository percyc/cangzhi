"""One bounded model call per task; claims/leases protect against late results."""
from datetime import timedelta
import uuid
from sqlalchemy import select

from apps.api.ai.provider import build_provider_from_session
from apps.api.models.enhancement import EnhancementRun, EnhancementWindow
from apps.api.models.processing import ProcessingJob
from apps.api.services import knowledge_enhancement as service


def finish_job(session, job_id, error=None):
    job = session.get(ProcessingJob, job_id, populate_existing=True)
    if job:
        job.status = "failed" if error else "completed"
        job.finished_at, job.next_retry_at = service.now(), None
        job.last_error, job.error_details = error, None


def fail_enhancement_job(session, job_id):
    """Record unexpected failure without damaging baseline document state."""
    job = session.get(ProcessingJob, job_id)
    message = "增强任务异常，请在文档增强面板检查并恢复"
    try:
        prefix, identifier = (job.config_version or "").split(":")
        if prefix == "enhancement":
            run = service.get_run(session, int(identifier), lock=True)
            if run.status in {"queued", "running"}:
                run.status, run.last_error = "failed", message
                run.active_attempt, run.lease_until = None, None
                for window in session.scalars(select(EnhancementWindow).where(
                        EnhancementWindow.run_id == run.id, EnhancementWindow.status == "running")):
                    window.status, window.last_error = "failed", message
    except (ValueError, service.EnhancementError):
        pass
    finish_job(session, job_id, message)
    session.commit()


def process_enhancement_job(session, job):
    job_id = job.id
    try:
        prefix, identifier = job.config_version.split(":")
        if prefix != "enhancement" or not identifier.isdigit():
            raise ValueError()
        run_id = int(identifier)
        run = service.get_run(session, run_id, lock=True)
        if run.document_id != job.document_id or run.document_version_id != job.document_version_id:
            raise ValueError()
    except (ValueError, service.EnhancementError):
        finish_job(session, job_id, "增强运行不存在或不可访问")
        session.commit()
        return False
    if run.status != "queued":
        finish_job(session, job_id)
        session.commit()
        return True
    if not service.source_is_current(session, run):
        run.status, run.last_error = "stale", "原文已变化，未继续分析"
        finish_job(session, job_id)
        session.commit()
        return True
    window = session.scalar(select(EnhancementWindow).where(
        EnhancementWindow.run_id == run.id, EnhancementWindow.status == "pending")
        .order_by(EnhancementWindow.ordinal).limit(1))
    if window is None or run.calls_used >= run.call_budget:
        summary = service.run_summary(session, run)
        run.status = "completed" if summary["completed_windows"] == run.total_windows else "partial"
        finish_job(session, job_id)
        session.commit()
        return True
    try:
        provider = build_provider_from_session(session)
    except Exception:
        provider = None
    if provider is None:
        run.status, run.last_error = "failed", "对话模型不可用，请检查模型设置后继续"
        finish_job(session, job_id, run.last_error)
        session.commit()
        return False
    if hasattr(provider, "_timeout"):
        provider._timeout = min(float(provider._timeout), 30.0)
    identity = {"provider": getattr(provider, "name", ""), "model": getattr(provider, "_model", ""),
                "prompt_version": "window-analysis:v1"}
    segments = service.source_segments(run, window)
    modules = run.config["modules"]
    token = str(uuid.uuid4())
    run.active_attempt, run.lease_until = token, service.now() + timedelta(seconds=180)
    run.status, run.last_error = "running", None
    run.calls_used += 1  # A crash or invalid response must not refund this attempt.
    window.status, window.model_identity = "running", identity
    window_id = window.id
    session.commit()  # No transaction is held while sending document text out.
    try:
        from apps.api.services.enhancement_analysis import analyze_window
        model_segments = [{key: segment[key] for key in ("id", "text", "block_id", "start", "stop")}
                          for segment in segments]
        result = analyze_window(provider, model_segments, modules)
        error = None
    except Exception:
        result, error = None, "模型调用失败或输出证据无效，可追加预算重试"
    try:
        run = service.get_run(session, run_id, lock=True)
    except service.EnhancementError:
        finish_job(session, job_id, "资料或工作空间已不可访问")
        session.commit()
        return False
    if run.active_attempt != token or run.status != "running":
        finish_job(session, job_id)
        session.commit()  # Cancelled/recovered attempt; discard late output.
        return True
    window = session.get(EnhancementWindow, window_id, populate_existing=True)
    run.active_attempt, run.lease_until = None, None
    if not service.source_is_current(session, run):
        run.status, run.last_error = "stale", "原文已变化，分析结果未发布"
        window.status, window.last_error = "failed", run.last_error
    else:
        window.status = "failed" if error else "completed"
        window.result, window.last_error = result, error
        session.flush()
        summary = service.run_summary(session, run)
        pending = session.scalar(select(EnhancementWindow.id).where(
            EnhancementWindow.run_id == run.id, EnhancementWindow.status == "pending").limit(1))
        if summary["completed_windows"] == run.total_windows:
            run.status = "completed"
        elif pending and run.calls_used < run.call_budget:
            run.status = "queued"
            service.enqueue_next(session, run)
        else:
            run.status = "partial" if summary["completed_windows"] else "failed"
            run.last_error = "存在未完成窗口，可追加预算继续；已完成内容保留"
    finish_job(session, job_id, error)
    session.commit()
    return error is None
