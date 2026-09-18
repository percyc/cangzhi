"""Shared synchronous domain service, also used through AsyncSession.run_sync.

Reads never invoke models. Source snapshots and outputs are supplementary and
never mutate basic summaries, taxonomy, chunks, vectors or source versions.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from sqlalchemy import func, select

from ..models.documents import Document, DocumentVersion
from ..models.enhancement import EnhancementRun, EnhancementWindow
from ..models.processing import ProcessingJob
from ..models.workspaces import Workspace
from .document_map import build_document_map, plan_source_windows
from .knowledge_enhancement_policy import EnhancementPolicy, POLICY_VERSION

STAGE = "knowledge_enhancement"
SETTINGS_KEY = "knowledge_enhancement"
SUPPORTED_MODULES = {"chapter", "graph"}
MAX_SOURCE_CHARS = 2_000_000
MAX_SOURCE_BLOCKS = 20000
MAX_RUN_CALLS = 256


class EnhancementError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def workspace(session, *, lock=False):
    workspace_id = session.info.get("cangzhi_workspace_id")
    if workspace_id is None:
        raise EnhancementError(403, "未指定工作空间")
    query = select(Workspace).where(Workspace.id == workspace_id, Workspace.status == "active")
    if lock:
        query = query.with_for_update()
    result = session.scalar(query.execution_options(populate_existing=True))
    if result is None:
        raise EnhancementError(404, "工作空间不存在或已归档")
    return result


def policy_for(space):
    try:
        policy = EnhancementPolicy.from_config((space.settings or {}).get(SETTINGS_KEY))
        if policy.modules - SUPPORTED_MODULES:
            raise ValueError("unsupported")
        return policy
    except (ValueError, TypeError):
        raise EnhancementError(409, "增强配置无效，请重新保存设置") from None


def policy_dict(policy):
    return {"enabled": policy.enabled, "modules": sorted(policy.modules),
            "call_budget": policy.call_budget, "cost_acknowledged": policy.cost_acknowledged,
            "effective_at": policy.effective_at.isoformat() if policy.effective_at else None}


def get_settings(session):
    return {**policy_dict(policy_for(workspace(session))), "supported_modules": sorted(SUPPORTED_MODULES)}


def save_settings(session, values):
    space = workspace(session, lock=True)
    old = policy_for(space)
    values = dict(values)
    if set(values) - {"enabled", "modules", "call_budget", "cost_acknowledged"}:
        raise EnhancementError(422, "不支持的增强配置项")
    # Enabling/re-enabling is forward-only; clients cannot backdate it.
    values["effective_at"] = old.effective_at if old.enabled else now()
    try:
        policy = EnhancementPolicy.from_config(values)
        if policy.modules - SUPPORTED_MODULES or (policy.enabled and not policy.modules):
            raise ValueError("unsupported_modules")
    except (ValueError, TypeError):
        raise EnhancementError(422, "请选择已支持的增强模块、有效预算并确认额外费用") from None
    space.settings = {**(space.settings or {}), SETTINGS_KEY: policy_dict(policy)}
    session.flush()
    return {**policy_dict(policy), "supported_modules": sorted(SUPPORTED_MODULES)}


def visible_document(session, document_id, *, lock=False):
    space = workspace(session)
    query = select(Document).where(Document.id == document_id,
        Document.workspace_id == space.id, Document.is_deleted.is_(False))
    if lock:
        query = query.with_for_update()
    document = session.scalar(query.execution_options(populate_existing=True))
    if document is None:
        raise EnhancementError(404, "资料不存在")
    return document


def get_run(session, run_id, *, lock=False):
    space = workspace(session)
    identity = session.execute(select(EnhancementRun.id, EnhancementRun.document_id).where(
        EnhancementRun.id == run_id, EnhancementRun.workspace_id == space.id)).first()
    if identity is None:
        raise EnhancementError(404, "增强记录不存在")
    # Consistent document -> run lock ordering with creation/publication.
    visible_document(session, identity.document_id, lock=lock)
    query = select(EnhancementRun).where(EnhancementRun.id == run_id,
                                         EnhancementRun.workspace_id == space.id)
    if lock:
        query = query.with_for_update()
    return session.scalar(query.execution_options(populate_existing=True))


def run_summary(session, run):
    counts = session.execute(select(EnhancementWindow.status, func.count(),
        func.coalesce(func.sum(EnhancementWindow.source_chars), 0)).where(
        EnhancementWindow.run_id == run.id).group_by(EnhancementWindow.status)).all()
    by_status = {status: (count, chars) for status, count, chars in counts}
    completed, chars = by_status.get("completed", (0, 0))
    return {"id": run.id, "document_id": run.document_id,
            "document_version_id": run.document_version_id, "status": run.status,
            "source_fingerprint": run.source_fingerprint, "call_budget": run.call_budget,
            "calls_used": run.calls_used, "total_windows": run.total_windows,
            "completed_windows": completed, "failed_windows": by_status.get("failed", (0, 0))[0],
            "total_source_chars": run.total_source_chars, "completed_source_chars": chars,
            "lease_until": aware(run.lease_until).isoformat() if run.lease_until else None,
            "last_error": run.last_error, "created_at": run.created_at.isoformat() if run.created_at else None}


def enqueue_next(session, run):
    key = f"enhancement:{run.id}:{run.calls_used}"
    if session.scalar(select(ProcessingJob.id).where(ProcessingJob.idempotency_key == key)) is None:
        session.add(ProcessingJob(document_id=run.document_id, document_version_id=run.document_version_id,
            stage=STAGE, status="created", config_version=f"enhancement:{run.id}",
            idempotency_key=key, max_retries=0))


def start_run(session, document_id, *, cost_acknowledged=False, automatic=False):
    document = visible_document(session, document_id, lock=True)
    policy = policy_for(workspace(session))
    if not policy.enabled or not policy.modules:
        if automatic:
            return None
        raise EnhancementError(409, "请先在当前工作空间启用 AI 知识增强")
    version = session.get(DocumentVersion, document.current_version_id)
    if version is None or not version.structured_content:
        raise EnhancementError(409, "请先完成正文解析")
    if automatic and not policy.should_auto_enhance(aware(version.created_at)):
        return None
    if not automatic and cost_acknowledged is not True:
        raise EnhancementError(422, "请确认额外模型调用和内容外发")
    payload = version.structured_content
    if payload.get("document_type") in {"xls", "xlsx", "database_table"}:
        raise EnhancementError(409, "数据集继续使用精确查询，不进行逐行知识增强")
    blocks = payload.get("blocks") or []
    if not isinstance(blocks, list) or any(not isinstance(b, dict) or
            not isinstance(b.get("text", ""), str) for b in blocks):
        raise EnhancementError(422, "正文结构不可用于增强，请检查解析结果")
    if len(blocks) > MAX_SOURCE_BLOCKS or sum(len(b.get("text", "")) for b in blocks) > MAX_SOURCE_CHARS:
        raise EnhancementError(422, "资料超过当前增强上限（2万块或200万字符），请按章节拆分")
    snapshot = copy.deepcopy({"document_type": payload.get("document_type", "txt"), "blocks": blocks})
    try:
        docmap = build_document_map(snapshot, version_id=version.id)
        windows = plan_source_windows(snapshot, version_id=version.id)
    except (TypeError, ValueError):
        raise EnhancementError(422, "正文结构不可用于增强，请检查解析结果") from None
    if not windows or len(windows) > 4096:
        raise EnhancementError(422, "正文为空或分析窗口过多，请检查解析结果")
    fingerprint = docmap["source_fingerprint"]
    config = {"modules": sorted(policy.modules), "policy_version": POLICY_VERSION,
              "analysis_version": "window-analysis:v1"}
    previous = session.scalars(select(EnhancementRun).where(
        EnhancementRun.document_id == document.id, EnhancementRun.document_version_id == version.id,
        EnhancementRun.source_fingerprint == fingerprint).order_by(EnhancementRun.id.desc())).all()
    for run in previous:
        if run.config == config:
            return run_summary(session, run)
    run = EnhancementRun(workspace_id=document.workspace_id, document_id=document.id,
        document_version_id=version.id, source_fingerprint=fingerprint, source_snapshot=snapshot,
        config=config, status="queued", call_budget=policy.call_budget, calls_used=0,
        total_windows=len(windows), total_source_chars=sum(w["source_chars"] for w in windows))
    session.add(run)
    session.flush()
    for ordinal, window in enumerate(windows):
        session.add(EnhancementWindow(run_id=run.id, ordinal=ordinal, status="pending",
            segments=window["segments"], source_chars=window["source_chars"]))
    enqueue_next(session, run)
    session.flush()
    return run_summary(session, run)


def list_runs(session, document_id):
    document = visible_document(session, document_id)
    runs = session.scalars(select(EnhancementRun).where(EnhancementRun.document_id == document_id)
                          .order_by(EnhancementRun.id.desc()).limit(20)).all()
    return {"current_version_id": document.current_version_id,
            "runs": [run_summary(session, run) for run in runs]}


def source_segments(run, window):
    result = []
    for index, segment in enumerate(window.segments):
        block = run.source_snapshot["blocks"][segment["block_index"]]
        result.append({"id": index, **segment,
            "text": block["text"][segment["start"]:segment["stop"]],
            "page": block.get("page"), "heading_path": block.get("heading_path", [])})
    return result


def read_run(session, run_id, *, offset=0, limit=5):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 10:
        raise EnhancementError(422, "分页范围无效")
    run = get_run(session, run_id)
    windows = session.scalars(select(EnhancementWindow).where(EnhancementWindow.run_id == run.id)
        .order_by(EnhancementWindow.ordinal).offset(offset).limit(limit)).all()
    return {"run": run_summary(session, run), "total_windows": run.total_windows,
        "next_offset": offset + len(windows) if offset + len(windows) < run.total_windows else None,
        "windows": [{"ordinal": window.ordinal, "status": window.status,
            "result": window.result, "last_error": window.last_error,
            "model_identity": window.model_identity, "source_segments": source_segments(run, window)}
            for window in windows]}


def source_is_current(session, run):
    document = session.get(Document, run.document_id, populate_existing=True)
    if document is None or document.is_deleted or document.current_version_id != run.document_version_id:
        return False
    version = session.get(DocumentVersion, run.document_version_id, populate_existing=True)
    try:
        return bool(version and version.structured_content and build_document_map(
            version.structured_content, version_id=version.id)["source_fingerprint"] == run.source_fingerprint)
    except (TypeError, ValueError):
        return False


def cancel_run(session, run_id):
    run = get_run(session, run_id, lock=True)
    if run.status not in {"completed", "stale"}:
        run.status, run.active_attempt, run.lease_until = "cancelled", None, None
        run.last_error = None
        for window in session.scalars(select(EnhancementWindow).where(
                EnhancementWindow.run_id == run.id, EnhancementWindow.status == "running")):
            window.status = "pending"
        session.flush()
    return run_summary(session, run)


def resume_run(session, run_id, *, additional_calls, cost_acknowledged):
    if cost_acknowledged is not True or type(additional_calls) is not int or not 1 <= additional_calls <= 32:
        raise EnhancementError(422, "追加预算必须是1–32次，并确认额外费用")
    run = get_run(session, run_id, lock=True)
    if run.status in {"queued", "completed", "stale"} or (
        run.status == "running" and run.lease_until and aware(run.lease_until) > now()):
        raise EnhancementError(409, "当前运行不能继续，请等待完成或执行租约到期")
    if not source_is_current(session, run):
        raise EnhancementError(409, "原文版本已变化，请为当前版本重新开始")
    if run.call_budget + additional_calls > MAX_RUN_CALLS:
        raise EnhancementError(422, "单次增强运行累计预算最多256次")
    run.call_budget += additional_calls
    run.active_attempt, run.lease_until, run.last_error = None, None, None
    run.status = "queued"
    for window in session.scalars(select(EnhancementWindow).where(
            EnhancementWindow.run_id == run.id, EnhancementWindow.status.in_(["failed", "running"]))):
        window.status, window.last_error = "pending", None
    # A model-free failure may already have consumed this scheduling identity.
    # Resume invalidates pending/stale jobs and uses a fresh budget generation.
    enqueue_resume(session, run)
    session.flush()
    return run_summary(session, run)


def enqueue_resume(session, run):
    key = f"enhancement:{run.id}:{run.calls_used}:budget:{run.call_budget}"
    session.add(ProcessingJob(document_id=run.document_id, document_version_id=run.document_version_id,
        stage=STAGE, status="created", config_version=f"enhancement:{run.id}",
        idempotency_key=key, max_retries=0))
