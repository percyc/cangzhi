"""Unit tests for the structure-prioritized chunker."""

from apps.api.parsers.base import Block, StructuredContent
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
    body = "这是第一句。".split("。")
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
