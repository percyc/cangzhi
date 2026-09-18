"""Workspace-scoped management service for non-destructive chunk previews."""
import asyncio
from sqlalchemy import select
from ..ai.provider import build_provider_from_db
from ..models.documents import Document, DocumentVersion
from .chunking_candidate import source_fingerprint, POLICY_VERSION as PDF_POLICY, build_candidate as build_pdf_candidate
from .chunking_adaptive import POLICY_VERSION as ADAPTIVE_POLICY, build_adaptive_candidate


def candidate_policy(payload):
    # Keep the deployed PDF path's historical false-heading repair. Adaptive v2
    # has not passed PDF quality comparisons and must not silently replace it.
    if payload.get("document_type") == "pdf":
        return PDF_POLICY, build_pdf_candidate
    return ADAPTIVE_POLICY, build_adaptive_candidate


class PreviewError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


async def preview_document(db, document_id: int, *, refresh: bool = False):
    document = await db.scalar(select(Document).where(Document.id == document_id, Document.is_deleted.is_(False)))
    if document is None:
        raise PreviewError(404, "资料不存在")
    version_id = document.current_version_id
    version = await db.get(DocumentVersion, version_id) if version_id else None
    if version is None or not version.structured_content:
        raise PreviewError(409, "请先完成正文解析")
    payload = version.structured_content
    policy_version, build_candidate = candidate_policy(payload)
    fingerprint = source_fingerprint(payload)
    provider = await build_provider_from_db(db)
    if provider is not None and hasattr(provider, "_timeout"):
        provider._timeout = min(float(provider._timeout), 10.0)
    identity = source_fingerprint({"provider": getattr(provider, "name", ""),
        "model": getattr(provider, "_model", ""), "base": getattr(provider, "_base_url", ""),
        "prompt": getattr(provider, "prompt_version", ""), "policy": policy_version})
    cached = (version.meta or {}).get("chunking_preview")
    if not refresh and isinstance(cached, dict) and cached.get("source_fingerprint") == fingerprint and cached.get("model_fingerprint") == identity:
        return {**cached, "cached": True}
    # Do not hold a database transaction across external model calls.
    await db.rollback()
    try:
        result = await asyncio.to_thread(build_candidate, payload, provider=provider, max_calls=2)
    except ValueError:
        raise PreviewError(422, "资料超过候选预览限制，需分章节处理") from None
    children = [s for s in result.pop("specs") if s["role"] == "child"]
    result.pop("groups", None)
    result.update(model_fingerprint=identity, document_version_id=version_id,
                  provider_available=provider is not None,
                  samples=[{"content": s["content"][:900], "page": s["page"],
                            "heading_path": s["heading_path"], "mode": s["extra"].get("boundary_mode", "rules")}
                           for s in children[:5]])
    document = await db.scalar(select(Document).where(Document.id == document_id, Document.is_deleted.is_(False)).with_for_update())
    if document is None or document.current_version_id != version_id:
        raise PreviewError(409, "资料已变化，请重新生成候选")
    version = await db.get(DocumentVersion, version_id, populate_existing=True)
    if source_fingerprint(version.structured_content) != fingerprint:
        raise PreviewError(409, "正文已变化，请重新生成候选")
    version.meta = {**(version.meta or {}), "chunking_preview": result}
    await db.commit()
    return result
