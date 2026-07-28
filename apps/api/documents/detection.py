"""Deterministic, conservative document-profile detection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, Sequence

DocumentType = Literal[
    "general", "legal", "contract", "paper", "meeting", "code", "table"
]

PROFILE_VERSION = "document-profile:v1"
_TYPES: tuple[DocumentType, ...] = (
    "general",
    "legal",
    "contract",
    "paper",
    "meeting",
    "code",
    "table",
)
_KEYWORDS: dict[DocumentType, tuple[str, ...]] = {
    "legal": (
        "人民法院",
        "法律规定",
        "行政法规",
        "司法解释",
        "诉讼请求",
        "裁判文书",
        "知识产权",
        "著作权",
        "专利权",
        "商标权",
    ),
    "contract": (
        "甲方",
        "乙方",
        "违约责任",
        "保密义务",
        "不可抗力",
        "争议解决",
        "签字盖章",
        "合同期限",
        "本协议",
        "双方约定",
    ),
    "paper": (
        "摘要",
        "关键词",
        "研究方法",
        "实验结果",
        "参考文献",
        "doi",
        "研究结论",
        "相关工作",
    ),
    "meeting": (
        "会议纪要",
        "参会人员",
        "主持人",
        "记录人",
        "会议议程",
        "会议决议",
        "行动项",
        "待办事项",
    ),
    "code": (),
    "table": (),
    "general": (),
}


@dataclass(frozen=True)
class DocumentProfile:
    detected_type: DocumentType
    confidence: float
    scores: dict[DocumentType, float]
    evidence: dict[str, float | str | int]
    profile_version: str = PROFILE_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


def detect_document_type(
    *,
    parser_type: str | None,
    block_types: Sequence[str],
    title: str | None,
    headings: Sequence[str],
    first_paragraphs: Sequence[str],
) -> DocumentProfile:
    """Return a stable profile; ambiguous content deliberately becomes general."""

    scores: dict[DocumentType, float] = {kind: 0.0 for kind in _TYPES}
    normalized_blocks = [str(value).lower() for value in block_types]
    total = len(normalized_blocks)
    code_count = sum(
        value in {"code", "code_block", "fenced_code"} for value in normalized_blocks
    )
    table_count = sum(value in {"table", "table_block"} for value in normalized_blocks)
    code_ratio = code_count / total if total else 0.0
    table_ratio = table_count / total if total else 0.0

    if code_count >= 1 and code_ratio >= 0.5:
        scores["code"] = 0.9
    elif code_count >= 2 and code_ratio >= 0.25:
        scores["code"] = 0.7
    if table_count >= 1 and table_ratio >= 0.5:
        scores["table"] = 0.9
    elif table_count >= 2 and table_ratio >= 0.25:
        scores["table"] = 0.7

    search_text = "\n".join(
        part.lower()
        for part in [
            title or "",
            *headings[:20],
            *(paragraph[:800] for paragraph in first_paragraphs[:8]),
        ]
        if part
    )
    evidence: dict[str, float | str | int] = {
        "parser_type": (parser_type or "unknown").lower(),
        "total_blocks": total,
        "code_blocks": code_count,
        "table_blocks": table_count,
        "code_ratio": round(code_ratio, 4),
        "table_ratio": round(table_ratio, 4),
    }

    for kind in ("legal", "contract", "paper", "meeting"):
        matches = sum(keyword in search_text for keyword in _KEYWORDS[kind])
        evidence[f"{kind}_keyword_matches"] = matches
        # Domain labels require at least two independent signals.
        if matches >= 4:
            scores[kind] = 0.9
        elif matches == 3:
            scores[kind] = 0.75
        elif matches == 2:
            scores[kind] = 0.6

    best_type, best_score = max(scores.items(), key=lambda item: item[1])
    competing_scores = sorted(scores.values(), reverse=True)
    margin = best_score - (competing_scores[1] if len(competing_scores) > 1 else 0.0)
    if best_score < 0.6 or margin < 0.1:
        detected_type: DocumentType = "general"
        confidence = round(max(0.5, 1.0 - best_score), 3)
        evidence["fallback_reason"] = (
            "low_confidence" if best_score < 0.6 else "ambiguous_profile"
        )
    else:
        detected_type = best_type
        confidence = round(min(best_score, 1.0), 3)

    evidence["best_type_before_fallback"] = best_type
    evidence["best_score_before_fallback"] = round(best_score, 3)
    return DocumentProfile(
        detected_type=detected_type,
        confidence=confidence,
        scores={key: round(value, 3) for key, value in scores.items()},
        evidence=evidence,
    )
