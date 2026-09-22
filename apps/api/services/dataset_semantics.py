"""Bounded, optional AI suggestions for dataset-field meaning.

The stored values are derivative hints.  Field names, inferred types, samples,
statistics and source rows remain the facts and are never modified here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from ..ai import AIProvider, AIProviderError
from ..models.datasets import DatasetField, KnowledgeDataset

MAX_FIELDS_PER_CALL = 40
MAX_AI_FIELDS = 320
MAX_DESCRIPTION_CHARS = 500
MAX_UNIT_CHARS = 64
MAX_ALIAS_CHARS = 80
MAX_ALIASES = 6

SYSTEM_PROMPT = """你是藏知的数据集字段语义助手。只返回 JSON 对象，不输出解释。
字段名、程序推断类型、统计和样例是只读事实；不得修改字段、编造数据字典、枚举含义、
单位或业务口径。结合数据集标题、表名、相邻字段和有界样例，为当前批次每个字段给出
简短说明、可能的单位、便于自然语言匹配的别名和置信度。无法可靠判断时，description
明确写“含义需结合来源确认”，unit 返回 null，confidence 应低于 0.5。
返回格式严格为 {"fields":[{"name":"原字段名","description":"...",
"unit":null,"aliases":[],"confidence":0.0}]}。只能返回当前批次给出的字段名。"""


@dataclass(frozen=True)
class SemanticEnrichmentResult:
    updated: int
    total: int
    status: str
    calls: int
    model: str | None


@dataclass(frozen=True)
class FieldSemanticProposal:
    suggestions: dict[str, dict[str, Any]]
    total: int
    target_names: tuple[str, ...]
    status: str
    calls: int
    provider: str
    model: str | None


def _clean_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split()).strip()
    return cleaned[:limit] or None


def validate_field_suggestions(
    payload: dict[str, Any], allowed_names: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Accept only exact, unique field names and bounded scalar metadata."""

    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list):
        raise ValueError("模型输出缺少 fields 数组")
    allowed = set(allowed_names)
    accepted: dict[str, dict[str, Any]] = {}
    for item in raw_fields:
        if not isinstance(item, dict) or set(item) - {
            "name",
            "description",
            "unit",
            "aliases",
            "confidence",
        }:
            continue
        name = item.get("name")
        if not isinstance(name, str) or name not in allowed or name in accepted:
            continue
        description = _clean_text(item.get("description"), MAX_DESCRIPTION_CHARS)
        if description is None:
            continue
        unit = _clean_text(item.get("unit"), MAX_UNIT_CHARS)
        aliases = item.get("aliases")
        clean_aliases: list[str] = []
        if isinstance(aliases, list):
            for alias in aliases:
                cleaned = _clean_text(alias, MAX_ALIAS_CHARS)
                if cleaned and cleaned != name and cleaned not in clean_aliases:
                    clean_aliases.append(cleaned)
                if len(clean_aliases) >= MAX_ALIASES:
                    break
        try:
            confidence = min(1.0, max(0.0, float(item.get("confidence"))))
        except (TypeError, ValueError):
            confidence = 0.0
        accepted[name] = {
            "description": description,
            "unit": unit,
            "aliases": clean_aliases,
            "confidence": round(confidence, 3),
        }
    return accepted


def generate_field_semantic_proposal(
    provider: AIProvider,
    dataset_context: dict[str, Any],
    field_contexts: Sequence[dict[str, Any]],
    *,
    target_names: Sequence[str] | None = None,
) -> FieldSemanticProposal:
    """Call the model using detached data so async callers stay thread-safe."""

    contexts = list(field_contexts)
    available_names = {str(field["name"]) for field in contexts}
    requested_names = (
        [str(name) for name in target_names if str(name) in available_names]
        if target_names is not None
        else [str(field["name"]) for field in contexts]
    )
    requested = set(requested_names)
    targets = [field for field in contexts if str(field["name"]) in requested]
    selected = targets[:MAX_AI_FIELDS]
    suggestions: dict[str, dict[str, Any]] = {}
    calls = 0
    model = str(getattr(provider, "_model", "") or "") or None
    all_names = [str(field["name"]) for field in contexts]
    for offset in range(0, len(selected), MAX_FIELDS_PER_CALL):
        batch = selected[offset : offset + MAX_FIELDS_PER_CALL]
        prompt = json.dumps(
            {
                "dataset": {
                    **dataset_context,
                    "all_field_names": all_names[:MAX_AI_FIELDS],
                },
                "current_fields": batch,
            },
            ensure_ascii=False,
            default=str,
        )
        payload = provider.generate_json(system=SYSTEM_PROMPT, prompt=prompt)
        calls += 1
        suggestions.update(
            validate_field_suggestions(
                payload, [str(field["name"]) for field in batch]
            )
        )
    return FieldSemanticProposal(
        suggestions=suggestions,
        total=len(targets),
        target_names=tuple(str(field["name"]) for field in targets),
        status="completed" if len(selected) == len(targets) else "partial",
        calls=calls,
        provider=provider.name,
        model=model,
    )


def field_semantic_context(field: DatasetField) -> dict[str, Any]:
    return {
        "name": field.name,
        "position": field.position,
        "inferred_type": field.inferred_type,
        "semantic_role": field.semantic_role,
        "null_count": field.null_count,
        "distinct_count": field.distinct_count,
        "sample_values": list(field.sample_values or [])[:8],
        "statistics": field.statistics or {},
    }


def apply_field_semantic_proposal(
    dataset: KnowledgeDataset,
    fields: Sequence[DatasetField],
    proposal: FieldSemanticProposal,
) -> SemanticEnrichmentResult:
    updated = 0
    for field in fields:
        suggestion = proposal.suggestions.get(field.name)
        if suggestion is not None:
            field.description = suggestion["description"]
            field.unit = suggestion["unit"]
            field.aliases = suggestion["aliases"]
            field.semantic_source = "ai"
            field.semantic_confidence = suggestion["confidence"]
            updated += 1

    profile = dict(dataset.profile or {})
    effective_status = (
        proposal.status if updated == proposal.total else "partial"
    )
    profile["field_semantics"] = {
        "status": effective_status,
        "updated_fields": updated,
        "target_fields": proposal.total,
        "total_fields": len(fields),
        "calls": proposal.calls,
        "source": "ai",
        "provider": proposal.provider,
        "model": proposal.model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limit": MAX_AI_FIELDS,
    }
    refresh = profile.get("field_semantics_refresh")
    if isinstance(refresh, dict):
        remaining = [
            name for name in proposal.target_names if name not in proposal.suggestions
        ]
        profile["field_semantics_refresh"] = {
            **refresh,
            "status": "completed" if not remaining else "partial",
            "estimated_ai_fields": len(remaining),
            "last_requested_fields": proposal.total,
            "last_updated_fields": updated,
        }
    dataset.profile = profile
    return SemanticEnrichmentResult(
        updated=updated,
        total=proposal.total,
        status=effective_status,
        calls=proposal.calls,
        model=proposal.model,
    )


def enrich_dataset_fields(
    provider: AIProvider,
    dataset: KnowledgeDataset,
    fields: Sequence[DatasetField],
    *,
    document_title: str,
    target_names: Sequence[str] | None = None,
) -> SemanticEnrichmentResult:
    """Synchronous convenience wrapper used by the Worker."""

    proposal = generate_field_semantic_proposal(
        provider,
        {
            "title": document_title,
            "name": dataset.name,
            "sheet_name": dataset.sheet_name,
            "row_count": dataset.row_count,
        },
        [field_semantic_context(field) for field in fields],
        target_names=target_names,
    )
    return apply_field_semantic_proposal(dataset, fields, proposal)


__all__ = [
    "AIProviderError",
    "SemanticEnrichmentResult",
    "FieldSemanticProposal",
    "apply_field_semantic_proposal",
    "enrich_dataset_fields",
    "field_semantic_context",
    "generate_field_semantic_proposal",
    "validate_field_suggestions",
]
