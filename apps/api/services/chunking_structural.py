"""Structure-only candidate; no model calls; never mutate the source payload.

This module is a sibling of :mod:`chunking_candidate` and
:mod:`chunking_adaptive`. It exists to keep the v1 (model-driven windows)
and v2 (risk-ranked windows) code paths untouched while a third, purely
structural candidate is built next to them. The candidate here does not
ask a model for boundary proposals: it rebuilds the whole corrected
``StructuredContent`` once and feeds it to :func:`build_chunk_specs`.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, replace
import unicodedata

from ..parsers.base import Block, StructuredContent
from .chunker import (
    CHILD_HARD_MAX_CHARS,
    CHILD_OVERLAP_CHARS,
    CHILD_TARGET_MAX_CHARS,
    CHILD_TARGET_MIN_CHARS,
    build_chunk_specs,
)
from .chunking_candidate import MAX_BLOCKS, source_fingerprint

POLICY_VERSION = "structural-v3"
DATASETS = {"xls", "xlsx", "database_table"}


def fragmentation_regressed(serving: dict, candidate: dict) -> bool:
    """Conservative maintenance veto, not a semantic quality score.

    Both more retrieval units and a higher short-unit share require review.
    Compare against the *serving* generation, not a reconstructed baseline.
    """
    old, new = serving["children"], candidate["children"]
    old_short, new_short = serving["short_children"], candidate["short_children"]
    return (old > 0 and new > old and new_short > old_short
            and new_short * old > old_short * new)


def _correct_pdf_legacy_headings(blocks: list[Block]) -> tuple[list[Block], int, int]:
    """Demote invalid legacy PDF headings to plain paragraphs in one pass.

    Only repair unannotated, legacy uppercase detections. Explicit/OCR
    metadata and ambiguous headings are retained conservatively.
    The original payload is never touched; a new list of blocks is
    returned with corrected ``level``, ``heading_path`` and ``type``.
    """
    from ..parsers.pdf import native_heading_level

    path: list[str] = []
    corrected: list[Block] = []
    demoted = kept = 0
    for block in blocks:
        level = native_heading_level(block.text) if block.type == "heading" else None
        normalized = unicodedata.normalize("NFKC", block.text)
        ascii_letters = sum("A" <= char <= "Z" for char in normalized)
        suspected_legacy = normalized.isupper() and (
            any(char.isdigit() for char in normalized) or ascii_letters <= 2
        )
        if block.type == "heading" and (
            block.extra or not suspected_legacy
        ):
            level = block.level or level or 1
        if level is not None:
            path = path[: max(0, level - 1)] + [block.text]
            corrected.append(
                replace(block, level=level, heading_path=list(path))
            )
            kept += 1
        else:
            corrected.append(
                replace(
                    block,
                    type="paragraph" if block.type == "heading" else block.type,
                    level=None,
                    heading_path=list(path),
                )
            )
            if block.type == "heading":
                demoted += 1
    return corrected, demoted, kept


def _coerce_blocks(payload: dict) -> list[Block]:
    raw = payload.get("blocks") or []
    coerced: list[Block] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        coerced.append(
            Block(
                type=entry.get("type") or "paragraph",
                text=entry.get("text") or "",
                heading_path=list(entry.get("heading_path") or []),
                page=entry.get("page"),
                paragraph_index=entry.get("paragraph_index"),
                level=entry.get("level"),
                extra=dict(entry.get("extra") or {}),
            )
        )
    return coerced


def _metrics(specs) -> dict:
    children = [s for s in specs if s.role == "child"]
    return {
        "children": len(children),
        "parents": sum(s.role == "parent" for s in specs),
        "short_children": sum(s.char_count < 100 for s in children),
    }


def _source_coverage(source: str, specs) -> dict:
    """Verify content, not just claimed spans. Whitespace-only gaps are harmless.

    Long parents may be explicitly abbreviated; their children must then
    preserve the missing text. Overlap prefixes are excluded from proof.
    """
    spans = []
    mismatches = 0
    for spec in specs:
        if spec.extra.get("parent_content_truncated"):
            continue
        start = spec.extra.get("core_source_start", spec.source_start)
        end = spec.extra.get("core_source_end", spec.source_end)
        prefix = spec.extra.get("overlap_prefix_chars", 0)
        content = spec.content[prefix + 2:] if prefix else spec.content
        # The shared chunker includes the inter-section separator in some
        # span ends (including a virtual final separator). Only verify the
        # actual content; never claim those extra characters as evidence.
        verified_end = start + len(content)
        if not (0 <= start <= verified_end <= len(source) and verified_end <= end) or source[start:verified_end] != content:
            mismatches += 1
            continue
        spans.append((start, verified_end))
    cursor = missing = 0
    for start, end in sorted(spans):
        if start > cursor:
            missing += sum(not char.isspace() for char in source[cursor:start])
        cursor = max(cursor, end)
    missing += sum(not char.isspace() for char in source[cursor:])
    return {"source_content_complete": missing == 0,
            "uncovered_nonspace_chars": missing,
            "unverified_spans": mismatches}


def build_structural_candidate(
    payload: dict,
    *,
    child_max_chars: int = CHILD_TARGET_MAX_CHARS,
    child_hard_max_chars: int = CHILD_HARD_MAX_CHARS,
    child_min_chars: int = CHILD_TARGET_MIN_CHARS,
    child_overlap_chars: int = CHILD_OVERLAP_CHARS,
) -> dict:
    """Build a model-free structural candidate and full coverage diagnostics.

    The candidate applies the legacy PDF heading correction in a single
    whole-document pass, then runs :func:`build_chunk_specs` once on the
    corrected ``StructuredContent``. Non-PDF formats keep their explicit
    headings verbatim; datasets reuse the baseline path unchanged. The
    original ``payload`` is never mutated.
    """
    if len(payload.get("blocks", [])) > MAX_BLOCKS:
        raise ValueError("too_many_blocks")

    fingerprint = source_fingerprint(payload)
    document_type = payload.get("document_type") or "txt"
    metadata = dict(payload.get("metadata") or {})
    sizing = dict(
        child_max_chars=child_max_chars,
        child_hard_max_chars=child_hard_max_chars,
        child_min_chars=child_min_chars,
        child_overlap_chars=child_overlap_chars,
    )

    raw_structured = StructuredContent(
        document_type=document_type,
        blocks=_coerce_blocks(payload),
        metadata=metadata,
    )
    baseline = build_chunk_specs(
        raw_structured, content_hash_seed=fingerprint, **sizing
    )

    skipped_dataset = raw_structured.document_type in DATASETS
    pdf_corrected = raw_structured.document_type == "pdf" and not skipped_dataset
    demoted = kept = 0
    if pdf_corrected:
        corrected_blocks, demoted, kept = _correct_pdf_legacy_headings(
            list(raw_structured.blocks)
        )
        corrected = replace(raw_structured, blocks=corrected_blocks)
    else:
        corrected = raw_structured

    candidate = baseline if skipped_dataset else build_chunk_specs(
        corrected,
        content_hash_seed=f"{fingerprint}:{POLICY_VERSION}",
        preserve_source_spans=True,
        **sizing,
    )

    for role in ("parent", "child"):
        for index, spec in enumerate(s for s in candidate if s.role == role):
            spec.order_index = index

    visible = [b for b in raw_structured.blocks if b.text and b.text.strip()]
    pages = sorted({b.page for b in visible if b.page is not None})
    block_type_counts = Counter(b.type for b in visible)
    heading_paths = [b.heading_path for b in visible if b.heading_path]
    heading_path_depths = [len(p) for p in heading_paths]
    source_text = "\n\n".join(b.text.strip() for b in visible)
    source_chars = len(source_text)
    baseline_metrics = _metrics(baseline)
    candidate_metrics = _metrics(candidate)
    candidate_text = "\n\n".join(
        s.content for s in candidate if s.role == "parent"
    )
    candidate_chars = len(candidate_text)
    # A length ratio can hide omitted text behind duplicated text. This
    # diagnostic is exact parent-text preservation, not retrieval recall.
    parent_text_complete = candidate_text == source_text
    coverage = 1.0 if parent_text_complete else None
    heading_total = sum(
        1 for b in visible if b.type == "heading"
    )

    return {
        "policy_version": POLICY_VERSION,
        "source_fingerprint": fingerprint,
        "status": "candidate",
        "activated": False,
        "calls": 0,
        "skipped_dataset": skipped_dataset,
        "pdf_corrected": pdf_corrected,
        "headings_demoted": demoted,
        "headings_kept": kept,
        "headings_total": heading_total,
        "total_blocks": len(visible),
        "block_type_counts": dict(block_type_counts),
        "pages_seen": pages,
        "page_count": len(pages),
        "heading_path_max_depth": max(heading_path_depths, default=0),
        "heading_path_count": len(heading_paths),
        "source_chars": source_chars,
        "candidate_chars": candidate_chars,
        "coverage": coverage,
        "parent_text_complete": parent_text_complete,
        **(_source_coverage(source_text, candidate) if not skipped_dataset else {
            "source_content_complete": None,
            "uncovered_nonspace_chars": None,
            "unverified_spans": None,
        }),
        "sizing": sizing,
        "baseline": baseline_metrics,
        "candidate": candidate_metrics,
        "groups": [{"start": 0, "end": max(0, len(visible) - 1), "mode": "structural"}],
        "specs": [asdict(s) for s in candidate],
    }
