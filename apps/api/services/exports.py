from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..api.schemas import DocumentResponse

_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def safe_export_stem(document: DocumentResponse) -> str:
    title = _UNSAFE_FILENAME.sub("-", document.title).strip(" .-")
    return f"{document.id:06d}-{(title or '未命名资料')[:100]}"


def safe_original_filename(filename: str | None, blob_id: int) -> str:
    basename = (filename or f"blob-{blob_id}").replace("\\", "/").split("/")[-1]
    cleaned = _UNSAFE_FILENAME.sub("-", basename).strip(" .-")
    return (cleaned or f"blob-{blob_id}")[:180]


def document_export_payload(document: DocumentResponse) -> dict[str, Any]:
    version = document.current_version
    return {
        "schema_version": "cangzhi.document.v1",
        "id": document.id,
        "title": document.title,
        "description": document.description,
        "source_type": document.source_type,
        "source_url": document.source_url,
        "origin": document.origin.model_dump(mode="json")
        if document.origin is not None
        else None,
        "is_deleted": document.is_deleted,
        "deleted_at": document.deleted_at.isoformat()
        if document.deleted_at
        else None,
        "delete_reason": document.delete_reason,
        "created_at": document.created_at.isoformat(),
        "updated_at": document.updated_at.isoformat(),
        "category": document.primary_category.model_dump(mode="json")
        if document.primary_category is not None
        else None,
        "categories": [
            category.model_dump(mode="json") for category in document.categories
        ],
        "tags": [tag.model_dump(mode="json") for tag in document.tags],
        "summary": document.summary.model_dump(mode="json")
        if document.summary is not None
        else None,
        "current_version": (
            {
                "id": version.id,
                "version_number": version.version_number,
                "content_hash": version.content_hash,
                "processing_status": version.processing_status,
                "created_at": version.created_at.isoformat(),
                "meta": version.meta,
                "raw_content": version.raw_content,
                "structured_content": version.structured_content,
                "original": version.blob.model_dump(mode="json")
                if version.blob is not None
                else None,
            }
            if version is not None
            else None
        ),
    }


def document_json_bytes(document: DocumentResponse) -> bytes:
    return (
        json.dumps(
            document_export_payload(document),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def document_markdown_bytes(document: DocumentResponse) -> bytes:
    version = document.current_version
    categories = [category.name for category in document.categories]
    tags = [tag.name for tag in document.tags]
    lines = [
        "---",
        f"title: {json.dumps(document.title, ensure_ascii=False)}",
        f"cangzhi_id: {document.id}",
        f"source_type: {json.dumps(document.source_type, ensure_ascii=False)}",
        f"source_url: {json.dumps(document.source_url, ensure_ascii=False)}",
        f"created_at: {json.dumps(document.created_at.isoformat())}",
        f"updated_at: {json.dumps(document.updated_at.isoformat())}",
        f"categories: {json.dumps(categories, ensure_ascii=False)}",
        f"tags: {json.dumps(tags, ensure_ascii=False)}",
        f"is_deleted: {'true' if document.is_deleted else 'false'}",
        "---",
        "",
        f"# {document.title}",
        "",
    ]
    if document.summary is not None and document.summary.summary.strip():
        lines.extend(["## 摘要", "", document.summary.summary.strip(), ""])
    content = version.raw_content if version is not None else None
    if content and content.strip():
        lines.extend(["## 正文", "", content.strip(), ""])
    elif version is not None and version.structured_content:
        lines.extend(
            [
                "## 解析内容",
                "",
                "```json",
                json.dumps(
                    version.structured_content,
                    ensure_ascii=False,
                    indent=2,
                ),
                "```",
                "",
            ]
        )
    else:
        lines.extend(["_当前资料没有可导出的正文。_", ""])
    lines.extend(
        [
            "---",
            f"由藏知导出于 {datetime.now(timezone.utc).isoformat()}",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")
