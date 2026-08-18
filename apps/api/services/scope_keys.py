"""Lightweight document selection scopes for trusted integrations.

The selection is described by a small :class:`DocumentSelection`
object: a list of opaque scope keys plus an explicit list of document
ids. The candidate set is the union of documents bound to any of the
scope keys and the requested document ids (deduplicated, restricted
to active, non-deleted documents). Other retrieval filters continue
to intersect with that candidate set.

The :func:`candidate_condition` helper turns a :class:`DocumentSelection`
into a SQL ``WHERE`` clause for the lower-level retrieval services.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import delete, exists, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from ..models.document_scope_keys import DocumentScopeKey
from ..models.documents import Document

MAX_SCOPE_KEYS = 100
MAX_SCOPE_KEY_LENGTH = 128
MAX_SELECTION_DOCUMENT_IDS = 200


@dataclass(frozen=True)
class DocumentSelection:
    """Optional candidate selection for Search/Ask/Deep/MCP.

    When ``scope_keys`` is provided, every document that carries any
    of those keys is a candidate. ``document_ids`` adds explicit
    candidates by id. The candidate set is the deduplicated union of
    both lists, restricted to active documents.
    """

    scope_keys: tuple[str, ...] = field(default_factory=tuple)
    document_ids: tuple[int, ...] = field(default_factory=tuple)

    @classmethod
    def coerce(
        cls,
        scope_keys: Sequence[str] | None,
        document_ids: Sequence[int] | None,
    ) -> DocumentSelection:
        cleaned_keys = tuple(
            _normalize_scope_key(item)
            for item in (scope_keys or [])
            if item is not None
        )
        cleaned_keys = tuple(sorted({key for key in cleaned_keys if key}))
        if len(cleaned_keys) > MAX_SCOPE_KEYS:
            raise ValueError(f"最多支持 {MAX_SCOPE_KEYS} 个 scope key")
        ids: list[int] = []
        seen_ids: set[int] = set()
        for raw in document_ids or []:
            if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
                raise ValueError("document_id 必须是正整数")
            if raw in seen_ids:
                continue
            seen_ids.add(raw)
            ids.append(raw)
        if len(ids) > MAX_SELECTION_DOCUMENT_IDS:
            raise ValueError(
                f"最多支持 {MAX_SELECTION_DOCUMENT_IDS} 个 document_id"
            )
        return cls(scope_keys=cleaned_keys, document_ids=tuple(ids))

    @property
    def is_empty(self) -> bool:
        return not self.scope_keys and not self.document_ids

    @property
    def is_active(self) -> bool:
        return bool(self.scope_keys) or bool(self.document_ids)


def _normalize_scope_key(value: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError("scope_key 必须是字符串")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("scope_key 不能为空")
    if len(cleaned) > MAX_SCOPE_KEY_LENGTH:
        raise ValueError(f"scope_key 最长 {MAX_SCOPE_KEY_LENGTH} 个字符")
    return cleaned


def normalize_scope_keys(values: Sequence[str] | None) -> list[str]:
    """Normalize, dedupe, and sort a list of scope keys for write APIs."""

    if not values:
        return []
    normalized = sorted({_normalize_scope_key(item) for item in values})
    if len(normalized) > MAX_SCOPE_KEYS:
        raise ValueError(f"每篇文档最多设置 {MAX_SCOPE_KEYS} 个 scope key")
    return normalized


def scope_keys_bind_documents(scope_keys: Sequence[str]) -> ColumnElement[bool]:
    """Return an EXISTS clause that matches docs with any of ``scope_keys``."""

    keys = list(dict.fromkeys(scope_keys))
    if not keys:
        # Fallback condition that never matches; callers should not invoke
        # this helper with an empty key list.
        return exists(
            select(DocumentScopeKey.id).where(DocumentScopeKey.id.is_(None))
        )
    return exists(
        select(DocumentScopeKey.id).where(
            DocumentScopeKey.document_id == Document.id,
            DocumentScopeKey.workspace_id == Document.workspace_id,
            DocumentScopeKey.scope_key.in_(keys),
        )
    )


def candidate_condition(
    selection: DocumentSelection | None,
) -> ColumnElement[bool] | None:
    """Return a SQL ``WHERE`` fragment that pins retrieval to the selection.

    ``None`` means "no candidate restriction; the entire workspace is
    eligible". When the selection is active, the candidate set is the
    deduplicated union of documents bound to any of the scope keys and
    the explicitly listed document ids.
    """

    if selection is None:
        return None
    if not selection.is_active:
        # An explicit-but-empty selection must never broaden into an
        # unrestricted workspace query, even if a non-HTTP caller bypasses
        # the boundary validation.
        return false()
    clauses: list[ColumnElement[bool]] = []
    if selection.scope_keys:
        clauses.append(scope_keys_bind_documents(selection.scope_keys))
    if selection.document_ids:
        clauses.append(Document.id.in_(list(selection.document_ids)))
    return or_(*clauses)


async def list_document_scope_keys(db: AsyncSession, document_id: int) -> list[str]:
    return list(
        (
            await db.scalars(
                select(DocumentScopeKey.scope_key)
                .where(DocumentScopeKey.document_id == document_id)
                .order_by(DocumentScopeKey.scope_key)
            )
        ).all()
    )


async def replace_document_scope_keys(
    db: AsyncSession, document: Document, values: Sequence[str] | None
) -> list[str]:
    keys = normalize_scope_keys(values)
    await db.execute(
        delete(DocumentScopeKey).where(DocumentScopeKey.document_id == document.id)
    )
    for key in keys:
        db.add(
            DocumentScopeKey(
                workspace_id=document.workspace_id,
                document_id=document.id,
                scope_key=key,
            )
        )
    await db.flush()
    return keys


__all__ = [
    "MAX_SCOPE_KEYS",
    "MAX_SCOPE_KEY_LENGTH",
    "MAX_SELECTION_DOCUMENT_IDS",
    "DocumentSelection",
    "candidate_condition",
    "list_document_scope_keys",
    "normalize_scope_keys",
    "replace_document_scope_keys",
    "scope_keys_bind_documents",
]
