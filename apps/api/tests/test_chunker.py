"""Unit tests for the structure-prioritized chunker."""

from apps.api.parsers.base import Block, StructuredContent
from apps.api.services import chunker as chunker_module
from apps.api.services.chunker import (
    build_chunk_specs,
    chunk_content_hash,
)


def _structured(blocks, metadata=None):
    return StructuredContent(
        document_type="markdown",
        blocks=blocks,
        metadata=metadata or {},
    )


def test_short_section_becomes_single_parent_and_child():
    blocks = [
        Block(
            type="heading",
            text="引言",
            heading_path=["引言"],
            level=1,
            paragraph_index=0,
        ),
        Block(
            type="paragraph",
            text="一句话介绍本节内容。",
            heading_path=["引言"],
            paragraph_index=1,
        ),
    ]
    specs = build_chunk_specs(_structured(blocks))
    roles = [spec.role for spec in specs]
    assert roles == ["parent", "child"]
    parent, child = specs
    assert parent.heading_path == ["引言"]
    assert parent.content.endswith("一句话介绍本节内容。")
    assert child.content == parent.content
    assert child.parent_external_id == parent.external_id
    assert child.page is None
    assert child.paragraph_index == 1


def test_ocr_lines_merge_page_bbox_and_confidence():
    blocks = [
        Block(
            type="paragraph",
            text="扫描文件第一行。",
            heading_path=[],
            page=2,
            paragraph_index=0,
            extra={
                "source": "ocr",
                "ocr_engine": "tesseract",
                "bbox": [10.0, 700.0, 200.0, 720.0],
                "confidence": 0.8,
            },
        ),
        Block(
            type="paragraph",
            text="扫描文件第二行。",
            heading_path=[],
            page=2,
            paragraph_index=1,
            extra={
                "source": "ocr",
                "ocr_engine": "tesseract",
                "bbox": [12.0, 670.0, 260.0, 695.0],
                "confidence": 0.9,
            },
        ),
    ]

    specs = build_chunk_specs(_structured(blocks), child_overlap_chars=0)
    parent, child = specs
    for spec in (parent, child):
        assert spec.extra["source"] == "ocr"
        assert spec.extra["bbox"] == [10.0, 670.0, 260.0, 720.0]
        assert spec.extra["confidence"] == 0.85


def test_level_one_headings_split_sections():
    blocks = [
        Block(type="heading", text="章节一", heading_path=["章节一"], level=1, paragraph_index=0),
        Block(type="paragraph", text="第一段。", heading_path=["章节一"], paragraph_index=1),
        Block(type="heading", text="章节二", heading_path=["章节二"], level=1, paragraph_index=2),
        Block(type="paragraph", text="第二段。", heading_path=["章节二"], paragraph_index=3),
    ]
    specs = build_chunk_specs(_structured(blocks))
    parents = [s for s in specs if s.role == "parent"]
    assert [p.heading_path for p in parents] == [["章节一"], ["章节二"]]


def test_subheading_updates_path_without_splitting_section():
    blocks = [
        Block(type="heading", text="一级", heading_path=["一级"], level=1, paragraph_index=0),
        Block(type="heading", text="二级", heading_path=["一级", "二级"], level=2, paragraph_index=1),
        Block(type="paragraph", text="正文。", heading_path=["一级", "二级"], paragraph_index=2),
    ]
    specs = build_chunk_specs(_structured(blocks))
    parents = [s for s in specs if s.role == "parent"]
    assert len(parents) == 1
    assert parents[0].heading_path == ["一级", "二级"]


def test_long_section_splits_by_sentence_boundaries():
    paragraphs = []
    for i in range(20):
        paragraphs.append(
            Block(
                type="paragraph",
                text=(
                    f"第{i+1}句讨论的是搜索体验。我们希望检索结果"
                    "能够精确地反映原始段落的位置。中文分句要"
                    "求自然，不能粗暴硬切。"
                ),
                heading_path=["长章节"],
                paragraph_index=i,
            )
        )
    blocks = [
        Block(type="heading", text="长章节", heading_path=["长章节"], level=1, paragraph_index=0),
        *paragraphs,
    ]
    specs = build_chunk_specs(_structured(blocks))
    children = [s for s in specs if s.role == "child"]
    # Each paragraph is ~80 chars; they should not all merge into one chunk.
    assert len(children) > 1
    # No child should cut inside a sentence.
    for child in children:
        assert not child.content.rstrip().endswith(("讨论", "检索", "反映", "要求"))
        # source span must be monotonic across the document
        assert child.source_start < child.source_end


def test_hard_split_preserves_content_hash_consistency():
    text = "一" * 5000
    blocks = [
        Block(type="paragraph", text=text, heading_path=[], paragraph_index=0),
    ]
    specs_a = build_chunk_specs(_structured(blocks))
    specs_b = build_chunk_specs(_structured(blocks))
    assert [s.content_hash for s in specs_a] == [s.content_hash for s in specs_b]
    assert [s.external_id for s in specs_a] == [s.external_id for s in specs_b]


def test_oversized_parent_is_bounded_but_children_keep_full_coverage(monkeypatch):
    monkeypatch.setattr(chunker_module, "PARENT_MAX_CHARS", 1_000)
    text = "甲" * 2_500 + "乙" * 2_500
    specs = build_chunk_specs(
        _structured([Block(type="table", text=text, heading_path=["大表"])]),
        child_overlap_chars=0,
    )

    parent = next(spec for spec in specs if spec.role == "parent")
    children = [spec for spec in specs if spec.role == "child"]
    assert len(parent.content) == 1_000
    assert parent.content.startswith("甲") and parent.content.endswith("乙")
    assert parent.extra["parent_content_truncated"] is True
    assert parent.extra["original_char_count"] == 5_000
    assert "".join(child.content for child in children) == text


def test_long_table_splits_only_between_rows():
    rows = [f"第{i}行\t" + chr(0x4E00 + i) * 120 for i in range(12)]
    blocks = [
        Block(
            type="table",
            text="\n".join(rows),
            heading_path=["明细"],
            paragraph_index=1,
        ),
    ]

    specs = build_chunk_specs(_structured(blocks), child_overlap_chars=0)
    children = [spec for spec in specs if spec.role == "child"]

    assert len(children) > 1
    reconstructed_rows = [
        row
        for child in children
        for row in child.content.splitlines()
    ]
    assert reconstructed_rows == rows
    assert all(child.chunk_type == "table" for child in children)


def test_spreadsheet_chunks_keep_row_ranges_and_disable_character_overlap():
    rows = [
        f"行 {number}｜订单号=NO-{number:04d}｜客户=客户{number}｜金额={number * 100}"
        for number in range(2, 82)
    ]
    blocks = [
        Block(
            type="heading",
            text="订单",
            heading_path=["订单"],
            level=1,
        ),
        Block(
            type="table",
            text="\n".join(rows),
            heading_path=["订单", "数据区域 1"],
            paragraph_index=1,
            extra={
                "sheet_name": "订单",
                "region_index": 1,
                "row_start": 2,
                "row_end": 81,
                "header_row": 1,
                "column_names": ["订单号", "客户", "金额"],
            },
        ),
    ]

    specs = build_chunk_specs(_structured(blocks))
    children = [spec for spec in specs if spec.role == "child"]

    assert len(children) > 1
    assert all(child.extra["overlap_prefix_chars"] == 0 for child in children)
    assert children[0].extra["row_start"] == 2
    assert children[-1].extra["row_end"] == 81
    assert all(child.content.startswith("行 ") for child in children)


def test_spreadsheet_document_uses_compact_dataset_catalog_chunks():
    table_blocks = []
    for block_index in range(100):
        row_start = block_index * 200 + 2
        row_end = row_start + 199
        table_blocks.append(
            Block(
                type="table",
                text=(
                    f"行 {row_start}｜状态=是｜金额={row_start}\n"
                    f"行 {row_end}｜状态=否｜金额={row_end}"
                ),
                heading_path=["明细", "数据区域 1"],
                paragraph_index=block_index,
                extra={
                    "sheet_name": "明细",
                    "region_index": 1,
                    "row_start": row_start,
                    "row_end": row_end,
                    "header_row": 1,
                    "column_names": ["状态", "金额"],
                },
            )
        )
    structured = StructuredContent(document_type="xlsx", blocks=table_blocks)

    specs = build_chunk_specs(structured, child_overlap_chars=0)

    assert len(specs) == 2
    parent, child = specs
    assert parent.role == "parent"
    assert child.role == "child"
    assert child.chunk_type == "dataset_catalog"
    assert child.extra["dataset_catalog"] is True
    assert child.extra["catalog_row_count"] == 200
    assert child.extra["column_names"] == ["状态", "金额"]
    assert child.extra["catalog_sample_count"] == 12
    assert "精确筛选、计数、求和、分组和排序" in child.content


def test_adjacent_children_include_bounded_semantic_overlap():
    text = "".join(
        f"第{i}句说明跨切片上下文不能丢失。" for i in range(1, 80)
    )
    blocks = [
        Block(type="paragraph", text=text, heading_path=[], paragraph_index=0),
    ]

    specs = build_chunk_specs(_structured(blocks))
    children = [spec for spec in specs if spec.role == "child"]

    assert len(children) > 1
    for previous, current in zip(children, children[1:]):
        overlap_chars = current.extra["overlap_prefix_chars"]
        assert 0 < overlap_chars <= 120
        prefix = current.content[:overlap_chars]
        assert previous.content.rstrip().endswith(prefix)
        assert current.char_count <= 900
        assert current.extra["core_source_start"] <= current.extra["core_source_end"]


def test_overlap_can_be_disabled_for_exact_core_coverage():
    text = "无标点内容" * 500
    blocks = [
        Block(type="paragraph", text=text, heading_path=[], paragraph_index=0),
    ]

    specs = build_chunk_specs(_structured(blocks), child_overlap_chars=0)
    children = [spec for spec in specs if spec.role == "child"]

    assert "".join(child.content for child in children) == text


def test_paragraph_offsets_increase():
    blocks = [
        Block(type="heading", text="A", heading_path=["A"], level=1, paragraph_index=0),
        Block(type="paragraph", text="第一段。", heading_path=["A"], paragraph_index=1),
        Block(type="paragraph", text="第二段。", heading_path=["A"], paragraph_index=2),
    ]
    specs = build_chunk_specs(_structured(blocks))
    children = [s for s in specs if s.role == "child"]
    starts = [c.source_start for c in children]
    assert starts == sorted(starts)


def test_chunk_idempotent_under_dict_payload():
    blocks = [
        Block(type="heading", text="A", heading_path=["A"], level=1, paragraph_index=0),
        Block(type="paragraph", text="正文。", heading_path=["A"], paragraph_index=1),
    ]
    structured = _structured(blocks).to_dict()
    first = build_chunk_specs(structured)
    second = build_chunk_specs(structured)
    assert [s.external_id for s in first] == [s.external_id for s in second]
    assert [s.content_hash for s in first] == [s.content_hash for s in second]


def test_empty_document_returns_no_chunks():
    structured = StructuredContent(document_type="markdown", blocks=[])
    assert build_chunk_specs(structured) == []


def test_page_and_paragraph_index_are_preserved():
    long_paragraph = (
        "这是一段非常长的内容，用于触发按句子切分。"
        "它必须超过默认的 soft target 才能强制生成多个子片段。"
        "再多加一些内容确保总长度大于 600 字符目标。"
        "这样切分器就会按段落或句子边界拆分。"
        "再加一些内容。再加一些内容。再加一些内容。"
    ) * 3
    blocks = [
        Block(
            type="paragraph",
            text=long_paragraph,
            heading_path=[],
            page=1,
            paragraph_index=2,
        ),
        Block(
            type="paragraph",
            text=long_paragraph,
            heading_path=[],
            page=2,
            paragraph_index=0,
        ),
    ]
    specs = build_chunk_specs(_structured(blocks))
    children = [s for s in specs if s.role == "child"]
    assert len(children) >= 2, "expected the long content to produce multiple children"
    pages = {c.page for c in children}
    assert 1 in pages
    assert 2 in pages


def test_short_content_keeps_page_boundaries():
    blocks = [
        Block(
            type="paragraph",
            text="第一页的短内容。",
            heading_path=[],
            page=1,
            paragraph_index=0,
        ),
        Block(
            type="paragraph",
            text="第二页的短内容。",
            heading_path=[],
            page=2,
            paragraph_index=0,
        ),
    ]
    specs = build_chunk_specs(_structured(blocks))
    children = [s for s in specs if s.role == "child"]
    assert [child.page for child in children] == [1, 2]


def test_chunk_content_hash_matches_helper():
    text = "hello world"
    assert chunk_content_hash(text) == chunk_content_hash(text)
    assert chunk_content_hash(text) != chunk_content_hash("hello world!")
