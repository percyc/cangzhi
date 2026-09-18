"""Unit tests for apps.api.services.fragment_context.recover_page.

The function is a pure bounded recovery helper: it inspects the blocks
of a single PDF page, applies a 60 percent short-text threshold, joins
the original block texts with the source sequence preserved, and
returns the joined context only when the anchor (with whitespace
collapsed) appears in it. It must never rewrite, normalize, dedupe or
cross page boundaries.
"""

from __future__ import annotations

import copy

from apps.api.services.fragment_context import (
    MAX_CONTEXT_CHARS,
    MAX_PAGE_BLOCKS,
    bounded_anchor_context,
    recover_page,
)


def test_budget_truncation_keeps_anchor_not_page_header():
    source = "页眉说明" * 200 + "\n额定\n速度\n25 km/h\n" + "后续内容" * 100
    result = bounded_anchor_context(source, "额定\n\n速度\n\n25 km/h", 100)
    assert len(result) <= 100
    assert "额定\n速度\n25 km/h" in result
    assert result.startswith("…") and result.endswith("…")
    assert bounded_anchor_context(source, "速度", 1) == "…"
    assert bounded_anchor_context("原文", "原文", 10) == "原文"


def _short_blocks(extra_texts=()):
    base = [
        {"type": "paragraph", "text": "适用范围", "page": 1},
        {"type": "paragraph", "text": "额定速度", "page": 1},
        {"type": "paragraph", "text": "类型", "page": 1},
        {"type": "paragraph", "text": "A", "page": 1},
        {"type": "paragraph", "text": "B", "page": 1},
        {"type": "paragraph", "text": "C", "page": 1},
        {"type": "paragraph", "text": "D", "page": 1},
        {"type": "paragraph", "text": "E", "page": 1},
    ]
    base.extend({"type": "paragraph", "text": t, "page": 1} for t in extra_texts)
    return base


class TestRecoverPagePreservation:
    def test_preserves_chinese_english_numbers_and_units(self) -> None:
        blocks = [
            {"type": "paragraph", "text": "中文段落", "page": 1},
            {"type": "paragraph", "text": "English section", "page": 1},
            {"type": "paragraph", "text": "123", "page": 1},
            {"type": "paragraph", "text": "km/h", "page": 1},
            {"type": "paragraph", "text": "α", "page": 1},
            {"type": "paragraph", "text": "质量", "page": 1},
            {"type": "paragraph", "text": "3500", "page": 1},
            {"type": "paragraph", "text": "kg", "page": 1},
        ]
        result = recover_page(blocks, "中文段落", page=1)
        assert result is not None
        assert "中文段落" in result
        assert "English section" in result
        assert "123" in result
        assert "km/h" in result
        assert "α" in result
        assert "3500" in result
        assert "kg" in result
        assert result == "\n".join(b["text"] for b in blocks)

    def test_preserves_end_exception_phrase(self) -> None:
        blocks = _short_blocks(["但通过其他等效认证的设备可以豁免。"])
        result = recover_page(blocks, "适用范围", page=1)
        assert result is not None
        assert "但通过其他等效认证的设备可以豁免。" in result
        assert result.endswith("但通过其他等效认证的设备可以豁免。")


class TestRecoverPageNormalLongParagraphs:
    def test_normal_prose_is_not_expanded(self) -> None:
        blocks = [{"type": "paragraph", "text": "完整的普通正文段落不应被当成碎片自动扩大。" * 3, "page": 1}
                  for _ in range(8)]
        assert recover_page(blocks, "完整的普通正文", page=1) is None

    def test_normal_long_paragraph_recovered_with_full_source_sequence(self) -> None:
        long_paragraph = "设备运行条件说明 " * 30
        blocks = [
            {"type": "paragraph", "text": "适用范围", "page": 1},
            {"type": "paragraph", "text": "额定速度", "page": 1},
            {"type": "paragraph", "text": "25 km/h", "page": 1},
            {"type": "paragraph", "text": "类型", "page": 1},
            {"type": "paragraph", "text": "A", "page": 1},
            {"type": "paragraph", "text": "B", "page": 1},
            {"type": "paragraph", "text": "C", "page": 1},
            {"type": "paragraph", "text": long_paragraph, "page": 1},
        ]
        result = recover_page(blocks, "适用范围", page=1)
        assert result is not None
        assert result == "\n".join(b["text"] for b in blocks)
        assert long_paragraph in result


class TestRecoverPageRejection:
    def test_wrong_page_in_any_block_rejected(self) -> None:
        blocks = _short_blocks()
        blocks[2] = {"type": "paragraph", "text": "类型", "page": 2}
        assert recover_page(blocks, "适用范围", page=1) is None

    def test_empty_anchor_rejected(self) -> None:
        assert recover_page(_short_blocks(), "", page=1) is None
        assert recover_page(_short_blocks(), " \n\t ", page=1) is None

    def test_non_matching_anchor_rejected(self) -> None:
        blocks = _short_blocks()
        assert recover_page(blocks, "完全找不到的锚点 xyz", page=1) is None

    def test_table_block_rejected(self) -> None:
        blocks = _short_blocks()
        blocks[0] = {"type": "table", "text": "适用范围", "page": 1}
        assert recover_page(blocks, "适用范围", page=1) is None

    def test_code_block_rejected(self) -> None:
        blocks = _short_blocks()
        blocks[0] = {"type": "code_block", "text": "适用范围", "page": 1}
        assert recover_page(blocks, "适用范围", page=1) is None

    def test_more_than_512_blocks_rejected(self) -> None:
        blocks = [
            {"type": "paragraph", "text": f"块 {i:04d}", "page": 1}
            for i in range(MAX_PAGE_BLOCKS + 1)
        ]
        assert len(blocks) == MAX_PAGE_BLOCKS + 1
        assert recover_page(blocks, "块 0000", page=1) is None

    def test_exactly_512_blocks_accepted(self) -> None:
        blocks = [
            {"type": "paragraph", "text": f"块 {i:04d}", "page": 1}
            for i in range(MAX_PAGE_BLOCKS)
        ]
        assert len(blocks) == MAX_PAGE_BLOCKS
        result = recover_page(blocks, "块 0000", page=1)
        assert result is not None
        assert "块 0000" in result

    def test_over_4000_chars_rejected(self) -> None:
        blocks = _short_blocks(["X" * 4100])
        assert recover_page(blocks, "适用范围", page=1) is None

    def test_exactly_4000_chars_accepted(self) -> None:
        short_total = sum(len(b["text"]) for b in _short_blocks())
        newlines = len(_short_blocks())
        long_len = MAX_CONTEXT_CHARS - short_total - newlines
        blocks = _short_blocks(["L" * long_len])
        result = recover_page(blocks, "适用范围", page=1)
        assert result is not None
        assert len(result) == MAX_CONTEXT_CHARS


class TestRecoverPageDeduplication:
    def test_duplicate_block_text_is_not_deduplicated(self) -> None:
        blocks = [
            {"type": "paragraph", "text": "适用范围", "page": 1},
            {"type": "paragraph", "text": "额定速度", "page": 1},
            {"type": "paragraph", "text": "适用范围", "page": 1},
            {"type": "paragraph", "text": "类型", "page": 1},
            {"type": "paragraph", "text": "A", "page": 1},
            {"type": "paragraph", "text": "B", "page": 1},
            {"type": "paragraph", "text": "C", "page": 1},
            {"type": "paragraph", "text": "D", "page": 1},
        ]
        result = recover_page(blocks, "适用范围", page=1)
        assert result is not None
        assert result.count("适用范围") == 2
        assert result == "\n".join(b["text"] for b in blocks)


class TestRecoverPageAnchorBoundary:
    def test_anchor_at_256_chars_accepted(self) -> None:
        long_text = "X" * 256
        blocks = [
            {"type": "paragraph", "text": "适用范围", "page": 1},
            {"type": "paragraph", "text": "额定速度", "page": 1},
            {"type": "paragraph", "text": "类型", "page": 1},
            {"type": "paragraph", "text": "A", "page": 1},
            {"type": "paragraph", "text": "B", "page": 1},
            {"type": "paragraph", "text": "C", "page": 1},
            {"type": "paragraph", "text": "D", "page": 1},
            {"type": "paragraph", "text": long_text, "page": 1},
        ]
        anchor = "X" * 256
        assert len(anchor) == 256
        result = recover_page(blocks, anchor, page=1)
        assert result is not None

    def test_anchor_at_257_chars_rejected(self) -> None:
        long_text = "X" * 257
        blocks = [
            {"type": "paragraph", "text": "适用范围", "page": 1},
            {"type": "paragraph", "text": "额定速度", "page": 1},
            {"type": "paragraph", "text": "类型", "page": 1},
            {"type": "paragraph", "text": "A", "page": 1},
            {"type": "paragraph", "text": "B", "page": 1},
            {"type": "paragraph", "text": "C", "page": 1},
            {"type": "paragraph", "text": "D", "page": 1},
            {"type": "paragraph", "text": long_text, "page": 1},
        ]
        anchor = "X" * 257
        assert len(anchor) == 257
        assert recover_page(blocks, anchor, page=1) is None


class TestRecoverPageInputImmutability:
    def test_blocks_list_is_not_mutated_when_recovered(self) -> None:
        blocks = _short_blocks(["附加说明。"])
        snapshot = copy.deepcopy(blocks)
        result = recover_page(blocks, "适用范围", page=1)
        assert result is not None
        assert blocks == snapshot

    def test_blocks_list_is_not_mutated_when_rejected(self) -> None:
        blocks = _short_blocks()
        blocks[0] = {"type": "table", "text": "适用范围", "page": 1}
        snapshot = copy.deepcopy(blocks)
        result = recover_page(blocks, "适用范围", page=1)
        assert result is None
        assert blocks == snapshot
