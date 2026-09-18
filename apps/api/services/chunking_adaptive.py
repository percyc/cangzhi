"""Structure-ranked boundary proposals; source data and v1 policy stay untouched."""
from __future__ import annotations

import json
from dataclasses import asdict

from ..parsers.base import Block, StructuredContent
from .chunker import (
    CHILD_HARD_MAX_CHARS, CHILD_OVERLAP_CHARS, CHILD_TARGET_MAX_CHARS,
    CHILD_TARGET_MIN_CHARS, build_chunk_specs,
)
from .chunking_candidate import (
    MAX_BLOCKS, MAX_CALLS, SYSTEM, WINDOW_BLOCKS, WINDOW_CHARS,
    _validate_ends, _windows, source_fingerprint,
)

POLICY_VERSION = "ai-boundaries:adaptive-v2"
DATASETS = {"xls", "xlsx", "database_table"}
TEXT_FORMATS = {"pdf", "doc", "docx", "markdown", "md", "txt", "html", "note"}


def evaluate_windows(blocks: list[Block], *, hard_max_chars=CHILD_HARD_MAX_CHARS) -> list[dict]:
    """Pure structural triage, not a claim that a passage is semantically wrong."""
    windows = []
    for start, stop in _windows(blocks):
        part = blocks[start:stop]
        paragraphs = [b for b in part if b.type == "paragraph"]
        reasons = []
        score = 0
        short = sum(len(b.text.strip()) < 100 for b in paragraphs)
        if len(paragraphs) >= 4 and short / len(paragraphs) >= 0.6:
            score += 3
            reasons.append("fragmented_paragraphs")
        if paragraphs and any(b.type == "table" for b in part):
            score += 4
            reasons.append("table_with_explanation")
        if len(paragraphs) >= 2 and not any(b.type == "heading" for b in part) and any(
            len(b.text) > hard_max_chars for b in paragraphs
        ):
            score += 2
            reasons.append("long_unstructured_paragraphs")
        exclusions = []
        if len(part) < 2:
            exclusions.append("single_block")
        if any(b.type == "code_block" for b in part):
            exclusions.append("code_preserved")
        if len(part) > WINDOW_BLOCKS or sum(len(b.text) + 2 for b in part) > WINDOW_CHARS:
            exclusions.append("window_limit")
        if any(len(b.text) > WINDOW_CHARS for b in part):
            exclusions.append("oversize_block_requires_separate_strategy")
        windows.append({"start": start, "stop": stop, "score": score,
                        "reasons": reasons, "exclusions": exclusions,
                        "eligible": score > 0 and not exclusions})
    return windows


def build_adaptive_candidate(
    payload: dict, *, provider=None, max_calls: int = 2,
    child_max_chars: int = CHILD_TARGET_MAX_CHARS,
    child_hard_max_chars: int = CHILD_HARD_MAX_CHARS,
    child_min_chars: int = CHILD_TARGET_MIN_CHARS,
    child_overlap_chars: int = CHILD_OVERLAP_CHARS,
) -> dict:
    """Rank eligible windows; keep whole-document baseline if no valid proposal.

    Only original block ranges are emitted. Pagination remains a hard window
    boundary; this service does not repair OCR, reading order or split one block.
    """
    if len(payload.get("blocks", [])) > MAX_BLOCKS:
        raise ValueError("too_many_blocks")
    fingerprint = source_fingerprint(payload)
    structured = StructuredContent(
        document_type=payload.get("document_type", "txt"),
        blocks=[Block(**block) for block in payload.get("blocks", [])],
        metadata=dict(payload.get("metadata", {})),
    )
    sizing = dict(child_max_chars=child_max_chars, child_hard_max_chars=child_hard_max_chars,
                  child_min_chars=child_min_chars, child_overlap_chars=child_overlap_chars)
    baseline = build_chunk_specs(structured, content_hash_seed=fingerprint, **sizing)
    blocks = [b for b in structured.blocks if b.text and b.text.strip()]
    skipped_dataset = structured.document_type in DATASETS
    supported = structured.document_type in TEXT_FORMATS
    windows = evaluate_windows(blocks, hard_max_chars=child_hard_max_chars) if supported else []
    eligible = sorted((w for w in windows if w["eligible"]), key=lambda w: (-w["score"], w["start"]))
    budget = max(0, min(int(max_calls), MAX_CALLS))
    calls = failed = assisted = 0
    accepted = []
    if provider is not None and supported and not skipped_dataset:
        for window in eligible[:budget]:
            start, stop = window["start"], window["stop"]
            request = [{"id": i, "type": blocks[i].type, "text": blocks[i].text}
                       for i in range(start, stop)]
            calls += 1
            try:
                response = provider.generate_json(system=SYSTEM, prompt=json.dumps({"blocks": request}, ensure_ascii=False))
                ends = _validate_ends(response, start, stop, blocks)
            except Exception:
                failed += 1  # Never persist provider exception bodies or credentials.
                continue
            accepted.append((start, stop, ends))
            assisted += stop - start

    specs = baseline
    groups = []
    if accepted:
        # Only successful windows create new boundaries. Do not repartition all
        # unselected windows simply because they were evaluated by the router.
        cursor = 0
        for start, stop, ends in sorted(accepted):
            if cursor < start:
                groups.append({"start": cursor, "end": start - 1, "mode": "rules"})
            first = start
            for end in ends:
                groups.append({"start": first, "end": end, "mode": "ai"})
                first = end + 1
            cursor = stop
        if cursor < len(blocks):
            groups.append({"start": cursor, "end": len(blocks) - 1, "mode": "rules"})
        offsets = []
        offset = 0
        for block in blocks:
            offsets.append(offset)
            offset += len(block.text.strip()) + 2
        specs = []
        for group in groups:
            start, end = group["start"], group["end"]
            part = StructuredContent(structured.document_type, blocks[start:end + 1], metadata=structured.metadata)
            built = build_chunk_specs(part, content_hash_seed=f"{fingerprint}:{POLICY_VERSION}:{start}", **sizing)
            for spec in built:
                if not spec.heading_path and blocks[start].heading_path:
                    spec.heading_path = list(blocks[start].heading_path)
                spec.source_start += offsets[start]
                spec.source_end += offsets[start]
                for key in ("core_source_start", "core_source_end"):
                    if key in spec.extra:
                        spec.extra[key] += offsets[start]
                spec.extra.update(candidate_policy=POLICY_VERSION, boundary_mode=group["mode"],
                                  block_start=start, block_end=end)
            specs.extend(built)
        for role in ("parent", "child"):
            for index, spec in enumerate(s for s in specs if s.role == role):
                spec.order_index = index

    def metrics(items):
        children = [s for s in items if s.role == "child"]
        return {"children": len(children), "parents": sum(s.role == "parent" for s in items),
                "short_children": sum(s.char_count < 100 for s in children)}

    # Boundary validity is necessary, not sufficient. Reject a proposal that
    # creates more fragments AND at least two additional tiny retrieval units.
    # This is a conservative structural guard, not a semantic quality score.
    baseline_metrics, candidate_metrics = metrics(baseline), metrics(specs)
    validated_windows = len(accepted)
    quality_rejected = bool(accepted) and (
        candidate_metrics["children"] > baseline_metrics["children"]
        and candidate_metrics["short_children"] >= baseline_metrics["short_children"] + 2
    )
    if quality_rejected:
        specs, groups, accepted, assisted = baseline, [], [], 0
        candidate_metrics = baseline_metrics

    decision = (
        "dataset_rules" if skipped_dataset else "unsupported_format" if not supported else
        ("rules_limited" if any(w["exclusions"] for w in windows) else "no_risk") if not eligible else "no_provider" if provider is None else
        "budget_exhausted" if not budget else "quality_fallback" if quality_rejected else
        "ai_applied" if accepted else "model_fallback"
    )
    return {
        "policy_version": POLICY_VERSION, "source_fingerprint": fingerprint,
        "status": "candidate", "activated": False, "decision": decision,
        "reasons": sorted({r for w in windows for r in w["reasons"] + w["exclusions"]}),
        "calls": calls, "accepted_windows": len(accepted), "failed_windows": failed,
        "validated_windows": validated_windows, "quality_rejected": quality_rejected,
        "assisted_blocks": assisted, "total_blocks": len(blocks),
        "coverage": assisted / len(blocks) if blocks else 0,
        "eligible_windows": len(eligible), "unassisted_eligible_windows": len(eligible) - len(accepted),
        "skipped_dataset": skipped_dataset, "baseline": baseline_metrics, "candidate": candidate_metrics,
        "specs": [asdict(s) for s in specs], "groups": groups,
    }
