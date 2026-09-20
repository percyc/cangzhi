"""Deterministic preparation and quality diagnostics for serving chunks.

This is part of the baseline ingestion path. It never calls a model and never
mutates parser output. PDF headings are revalidated so documents parsed by an
older heuristic converge to the current rules when explicitly reprocessed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Sequence

from ..parsers.base import Block, StructuredContent
from .chunker import ChunkSpec

POLICY_VERSION = "baseline-structure:v1"


def prepare_structured_for_chunking(
    structured: StructuredContent,
) -> tuple[StructuredContent, dict]:
    """Return a source-preserving serving view and bounded diagnostics."""

    if structured.document_type != "pdf":
        return structured, {
            "policy_version": POLICY_VERSION,
            "mode": "rules",
            "model_calls": 0,
            "headings_total": sum(
                block.type == "heading" for block in structured.blocks
            ),
            "headings_kept": None,
            "headings_demoted": 0,
        }

    from ..parsers.pdf import native_heading_level

    candidate_levels = {
        index: native_heading_level(block.text)
        for index, block in enumerate(structured.blocks)
        if block.type == "heading"
    }
    heading_pages: dict[str, set[int | None]] = {}
    for index, level in candidate_levels.items():
        if level is None:
            continue
        block = structured.blocks[index]
        heading_pages.setdefault(block.text.strip(), set()).add(block.page)
    repeated_heading_texts = {
        text for text, pages in heading_pages.items() if len(pages) >= 2
    }

    path: list[str] = []
    prepared: list[Block] = []
    headings_total = headings_kept = headings_demoted = repeated_demoted = 0
    for index, block in enumerate(structured.blocks):
        if block.type == "heading":
            headings_total += 1
            level = candidate_levels.get(index)
            if level is not None and block.text.strip() in repeated_heading_texts:
                level = None
                repeated_demoted += 1
            if level is None:
                headings_demoted += 1
                prepared.append(
                    replace(
                        block,
                        type="paragraph",
                        level=None,
                        heading_path=list(path),
                    )
                )
                continue
            headings_kept += 1
            path = path[: max(level - 1, 0)] + [block.text]
            prepared.append(replace(block, level=level, heading_path=list(path)))
            continue
        prepared.append(replace(block, heading_path=list(path)))

    return replace(structured, blocks=prepared), {
        "policy_version": POLICY_VERSION,
        "mode": "normalized_rules",
        "model_calls": 0,
        "headings_total": headings_total,
        "headings_kept": headings_kept,
        "headings_demoted": headings_demoted,
        "repeated_headings_demoted": repeated_demoted,
    }


def page_safe_structured(structured: StructuredContent) -> StructuredContent:
    """Keep every source block but stop uncertain headings opening sections."""

    return replace(
        structured,
        blocks=[
            replace(
                block,
                type="paragraph" if block.type == "heading" else block.type,
                level=None if block.type == "heading" else block.level,
                heading_path=[],
            )
            for block in structured.blocks
        ],
    )


def assess_chunk_quality(
    structured: StructuredContent,
    specs: Sequence[ChunkSpec],
    preparation: dict,
    *,
    minimum_chars: int,
) -> dict:
    """Describe serving output without treating a heuristic as correctness."""

    children = [spec for spec in specs if spec.role == "child"]
    parents = [spec for spec in specs if spec.role == "parent"]
    short = sum(spec.char_count < minimum_chars for spec in children)
    pages = {
        block.page
        for block in structured.blocks
        if block.page is not None and block.text and block.text.strip()
    }
    repeated_headings = sum(
        count - 1
        for count in Counter(
            tuple(spec.heading_path) for spec in parents if spec.heading_path
        ).values()
        if count > 1
    )
    issues: list[str] = []
    if children and short / len(children) >= 0.2:
        issues.append("many_short_chunks")
    if pages and len(parents) > max(12, len(pages) * 2):
        issues.append("dense_section_boundaries")
    if repeated_headings:
        issues.append("repeated_headings")
    if (
        len(pages) >= 5
        and preparation.get("headings_total", 0) > 0
        and preparation.get("headings_kept") == 0
    ):
        issues.append("no_reliable_headings")

    score = 100
    if children:
        score -= round(45 * short / len(children))
    score -= min(repeated_headings * 2, 20)
    if "dense_section_boundaries" in issues:
        score -= 15
    if "no_reliable_headings" in issues:
        score -= 10
    score = max(score, 0)
    level = (
        "good"
        if score >= 85 and not issues
        else "fallback"
        if score < 55
        else "review"
    )
    return {
        **preparation,
        "quality_level": level,
        "quality_score": score,
        "issues": issues,
        "children": len(children),
        "parents": len(parents),
        "short_children": short,
        "page_count": len(pages),
        "repeated_headings": repeated_headings,
        "minimum_chars": minimum_chars,
    }
