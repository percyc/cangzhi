"""Tests for the structure-only structural-v3 candidate."""
import copy
import inspect
import json

import pytest

from apps.api.parsers.base import Block, StructuredContent
from apps.api.services.chunker import build_chunk_specs
from apps.api.services.chunking_structural import (
    POLICY_VERSION,
    _correct_pdf_legacy_headings,
    _source_coverage,
    build_structural_candidate,
    fragmentation_regressed,
)


def _payload(document_type, blocks, metadata=None):
    return StructuredContent(
        document_type=document_type,
        blocks=blocks,
        metadata=metadata or {},
    ).to_dict()


def _row_text(row, fragments=4):
    return f"行 {row}｜订单号=NO-{row:04d}｜明细={'数据' * 40}｜片段 {row}/{fragments}"


def test_policy_metadata_is_candidate_and_never_activates():
    source = _payload(
        "pdf",
        [Block("paragraph", f"原文块 {i}：事实与条件。", [], page=1,
               paragraph_index=i) for i in range(4)],
    )
    result = build_structural_candidate(source)
    assert result["policy_version"] == POLICY_VERSION == "structural-v3"
    assert result["status"] == "candidate"
    assert result["activated"] is False
    assert result["calls"] == 0
    assert result["source_fingerprint"]
    assert {"children", "parents", "short_children"} <= set(result["baseline"])
    assert {"children", "parents", "short_children"} <= set(result["candidate"])


@pytest.mark.parametrize("old,old_short,new,new_short,expected", [
    (100, 10, 120, 30, True),
    (100, 10, 120, 12, False),
    (100, 10, 80, 10, False),
    (100, 20, 120, 22, False),
    (0, 0, 120, 30, False),
    (100, 10, 100, 10, False),
])
def test_fragmentation_veto_requires_joint_regression(old, old_short, new, new_short, expected):
    assert fragmentation_regressed({"children": old, "short_children": old_short},
        {"children": new, "short_children": new_short}) is expected


def test_payload_is_never_mutated_for_word_or_markdown():
    blocks = [
        Block("heading", "引言", ["引言"], level=1, page=1, paragraph_index=0),
        Block("paragraph", "本节内容。", ["引言"], page=1, paragraph_index=1),
        Block("list_item", "第一项", ["引言"], page=1, paragraph_index=2),
        Block("code_block", "print('ok')", ["引言"], page=1, paragraph_index=3),
    ]
    for kind in ("docx", "markdown", "txt"):
        source = _payload(kind, blocks)
        before = copy.deepcopy(source)
        result = build_structural_candidate(source)
        assert source == before, kind
        # Output must be JSON serializable; no leaked binary or locals.
        json.dumps(result, ensure_ascii=False)


def test_word_and_markdown_preserve_explicit_headings():
    blocks = [
        Block("heading", "章节", ["章节"], level=1, page=1, paragraph_index=0),
        Block("heading", "小节", ["章节", "小节"], level=2, page=1, paragraph_index=1),
        Block("paragraph", "正文内容。", ["章节", "小节"], page=1, paragraph_index=2),
        Block("list_item", "要点一", ["章节", "小节"], page=1, paragraph_index=3),
        Block("code_block", "x = 1", ["章节", "小节"], page=1, paragraph_index=4),
        Block("table", "列1|列2\nA|B", ["章节", "小节"], page=1, paragraph_index=5),
    ]
    for kind in ("docx", "markdown"):
        result = build_structural_candidate(_payload(kind, blocks))
        assert result["pdf_corrected"] is False
        assert result["headings_demoted"] == 0
        assert result["headings_total"] == 2
        parents = [s for s in result["specs"] if s["role"] == "parent"]
        joined = "\n\n".join(s["content"] for s in parents)
        for marker in ("章节", "小节", "要点一", "x = 1", "列1|列2"):
            assert marker in joined, (kind, marker)
        assert result["block_type_counts"]["heading"] == 2
        assert result["block_type_counts"]["list_item"] == 1
        assert result["block_type_counts"]["code_block"] == 1
        assert result["block_type_counts"]["table"] == 1


def test_pdf_invalid_uppercase_heading_is_demoted_but_source_preserved():
    blocks = [
        Block("heading", "GB 14762 2008", ["GB 14762 2008"],
              level=1, page=1, paragraph_index=0),
        *(Block("paragraph", f"正文 {i}。", ["GB"], page=1, paragraph_index=1 + i)
          for i in range(3)),
        Block("heading", "ISO 9001 2015", ["ISO 9001 2015"],
              level=1, page=1, paragraph_index=4),
        Block("paragraph", "另一段。", ["ISO"], page=1, paragraph_index=5),
    ]

    source = _payload("pdf", blocks)
    before = copy.deepcopy(source)
    result = build_structural_candidate(source)
    assert source == before
    assert result["pdf_corrected"] is True
    assert result["headings_demoted"] == 2
    assert result["headings_kept"] == 0
    # Two false headings => two extra sections in the baseline; one section after.
    assert result["baseline"]["parents"] >= 2
    assert result["candidate"]["parents"] < result["baseline"]["parents"]
    # The false-heading text is still recoverable from the parent content.
    parents = [s for s in result["specs"] if s["role"] == "parent"]
    joined = "\n\n".join(s["content"] for s in parents)
    assert "GB 14762 2008" in joined
    assert "ISO 9001 2015" in joined


def test_pdf_real_heading_is_kept_and_drive_section_count():
    blocks = [
        Block("heading", "Chapter 1 Introduction",
              ["Chapter 1 Introduction"], level=1, page=1, paragraph_index=0),
        Block("paragraph", "正文一。", ["Chapter 1 Introduction"],
              page=1, paragraph_index=1),
        Block("heading", "Chapter 2 Method", ["Chapter 2 Method"],
              level=1, page=2, paragraph_index=2),
        Block("paragraph", "正文二。", ["Chapter 2 Method"],
              page=2, paragraph_index=3),
    ]
    source = _payload("pdf", blocks)
    result = build_structural_candidate(source)
    assert result["pdf_corrected"] is True
    assert result["headings_kept"] == 2
    assert result["headings_demoted"] == 0
    assert result["candidate"]["parents"] == 2
    assert result["page_count"] == 2
    assert result["pages_seen"] == [1, 2]
    # Page locator must reach each section.
    parents = [s for s in result["specs"] if s["role"] == "parent"]
    assert [p["page"] for p in parents] == [1, 2]


def test_pdf_fragmented_table_rows_remain_complete():
    rows = [_row_text(r) for r in range(2, 20)]
    blocks = [
        Block("heading", "Chapter 1 Detail", ["Chapter 1 Detail"],
              level=1, page=1, paragraph_index=0),
        Block("table", "\n".join(rows), ["Chapter 1 Detail"],
              page=1, paragraph_index=1),
        Block("paragraph", "小结。", ["Chapter 1 Detail"],
              page=1, paragraph_index=2),
    ]
    source = _payload("pdf", blocks)
    result = build_structural_candidate(source)
    parent_text = "\n\n".join(
        s["content"] for s in result["specs"] if s["role"] == "parent"
    )
    # Every row locator must survive the structural pass.
    for row in rows:
        assert row.split("｜")[0] in parent_text
    coverage_payload_chars = sum(len(row) + 1 for row in rows)
    assert result["candidate_chars"] >= coverage_payload_chars - 5
    # Children re-split the fragmented rows; coverage stays full.
    assert result["coverage"] >= 0.99


@pytest.mark.parametrize("kind", ["xls", "xlsx", "database_table"])
def test_datasets_reuse_baseline_path(kind):
    rows = [f"行 {n}｜字段A=值{n}｜字段B=值{n * 2}" for n in range(1, 30)]
    blocks = [
        Block("heading", "数据集说明", ["数据集说明"],
              level=1, page=1, paragraph_index=0),
        Block("table", "\n".join(rows), ["数据集说明"], page=1,
              paragraph_index=1, extra={
                  "sheet_name": "Sheet1",
                  "region_index": 1,
                  "row_start": 1,
                  "row_end": 30,
                  "column_names": ["字段A", "字段B"],
              }),
    ]
    source = _payload(kind, blocks)
    result = build_structural_candidate(source)
    assert result["skipped_dataset"] is True
    assert result["calls"] == 0
    # The candidate specs match the dataset baseline byte-for-byte.
    baseline = build_chunk_specs(
        StructuredContent(
            document_type=kind,
            blocks=[Block(**b) for b in source["blocks"]],
            metadata=source.get("metadata") or {},
        ),
        content_hash_seed=result["source_fingerprint"],
    )
    assert [s["external_id"] for s in result["specs"]] == [
        s.external_id for s in baseline
    ]


def test_long_source_is_fully_covered_and_deterministic():
    blocks = []
    for i in range(120):
        blocks.append(
            Block("paragraph", f"句子{i}：长文本。", ["长章节"],
                  page=1 + (i // 40), paragraph_index=i)
        )
    source = _payload("markdown", blocks)
    a = build_structural_candidate(source)
    b = build_structural_candidate(copy.deepcopy(source))
    assert a["specs"] == b["specs"]
    assert a["source_fingerprint"] == b["source_fingerprint"]
    source_text = "\n\n".join(b.text.strip() for b in blocks)
    assert a["source_chars"] == len(source_text) == a["candidate_chars"]
    assert a["coverage"] == 1.0
    parents = [s for s in a["specs"] if s["role"] == "parent"]
    assert len(parents) == 1
    for spec in a["specs"]:
        assert spec["source_start"] < spec["source_end"]


def test_source_locators_track_page_and_paragraph_index():
    blocks = [
        Block("heading", "Chapter 1", ["Chapter 1"], level=1, page=2,
              paragraph_index=5),
        Block("paragraph", "正文。", ["Chapter 1"], page=2,
              paragraph_index=6),
        Block("heading", "Chapter 2", ["Chapter 2"], level=1, page=7,
              paragraph_index=7),
        Block("paragraph", "另一段。", ["Chapter 2"], page=7,
              paragraph_index=8),
    ]
    source = _payload("docx", blocks)
    result = build_structural_candidate(source)
    parents = [s for s in result["specs"] if s["role"] == "parent"]
    assert [p["page"] for p in parents] == [2, 7]
    assert [p["paragraph_index"] for p in parents] == [5, 7]
    # No spec should claim a page outside the document's range.
    for spec in result["specs"]:
        assert spec["page"] in (None, 2, 7)
    # Parent source spans are monotonic across the document.
    parent_starts = [p["source_start"] for p in parents]
    assert parent_starts == sorted(parent_starts)
    # Each child has a valid span within its own section.
    for spec in result["specs"]:
        assert spec["source_start"] < spec["source_end"]


def test_groups_record_single_structural_pass():
    source = _payload(
        "markdown",
        [Block("paragraph", f"段{i}。", [], page=1, paragraph_index=i)
         for i in range(5)],
    )
    result = build_structural_candidate(source)
    assert result["groups"] == [
        {"start": 0, "end": 4, "mode": "structural"}
    ]


def test_does_not_split_into_32_block_windows():
    from apps.api.services.chunking_candidate import build_candidate
    blocks = [
        Block("paragraph", f"短句{i}。", [], page=1, paragraph_index=i)
        for i in range(80)
    ]
    source = _payload("pdf", blocks)
    result = build_structural_candidate(source)
    # structural-v3 is a single whole-document pass; no 32-block seams.
    assert len(result["groups"]) == 1
    assert result["calls"] == 0
    assert result["groups"][0]["mode"] == "structural"
    assert result["candidate"]["parents"] == 1
    assert result["candidate"]["children"] < build_candidate(source, max_calls=0)["candidate"]["children"]


def test_too_many_blocks_raises():
    payload = {
        "document_type": "txt",
        "blocks": [
            {"type": "paragraph", "text": "x", "heading_path": []}
            for _ in range(20001)
        ],
    }
    with pytest.raises(ValueError, match="too_many_blocks"):
        build_structural_candidate(payload)


def test_correct_helper_demotes_invalid_legacy_headings():
    blocks = [
        Block("heading", "GLOBAL OVERVIEW", [], level=1),
        Block("paragraph", "正文", ["GLOBAL OVERVIEW"]),
        Block("heading", "ISO 9001 2015", [], level=1),
        Block("paragraph", "另一段", []),
    ]
    corrected, demoted, kept = _correct_pdf_legacy_headings(blocks)
    # "GLOBAL OVERVIEW" matches the legacy all-uppercase phrase rule.
    assert kept == 1
    assert demoted == 1
    assert corrected[0].type == "heading"
    assert corrected[2].type == "paragraph"
    assert corrected[2].level is None


def test_no_provider_kwarg_is_offered_to_callers():
    sig = inspect.signature(build_structural_candidate)
    assert "provider" not in sig.parameters
    assert set(sig.parameters) >= {
        "payload", "child_max_chars", "child_hard_max_chars",
        "child_min_chars", "child_overlap_chars",
    }


def test_pdf_explicit_and_ambiguous_headings_survive():
    blocks = [
        Block("heading", "摘要", [], level=1),
        Block("paragraph", "内容。", []),
        Block("heading", "API", [], level=2, extra={"source": "ocr"}),
        Block("paragraph", "接口说明。", []),
        Block("heading", "INTRODUCTION", [], level=1),
    ]
    corrected, demoted, kept = _correct_pdf_legacy_headings(blocks)
    assert demoted == 0 and kept == 3
    assert corrected[0].type == corrected[2].type == "heading"
    assert corrected[2].level == 2


def test_coverage_does_not_trust_false_spans_or_equal_text_lengths():
    from types import SimpleNamespace
    spec = SimpleNamespace(source_start=0, source_end=4, content="伪造内容", extra={})
    result = _source_coverage("真实原文", [spec])
    assert result["source_content_complete"] is False
    assert result["uncovered_nonspace_chars"] == 4


def test_long_parent_abbreviation_is_covered_by_children():
    source = _payload("txt", [Block("paragraph", f"段落{i}。" * 70, []) for i in range(300)])
    result = build_structural_candidate(source)
    assert result["parent_text_complete"] is False
    assert result["coverage"] is None
    assert result["source_content_complete"] is True
    assert result["uncovered_nonspace_chars"] == 0


def test_candidate_specs_serializable_to_json():
    source = _payload(
        "markdown",
        [Block("paragraph", "短", ["A"], page=1, paragraph_index=0)],
    )
    result = build_structural_candidate(source)
    payload = json.dumps(result, ensure_ascii=False)
    assert "structural-v3" in payload
    assert "candidate" in payload
    assert "source_fingerprint" in payload
