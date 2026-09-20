from apps.api.parsers.base import Block, StructuredContent
from apps.api.parsers.pdf import native_heading_level
from apps.api.services.chunker import build_chunk_specs
from apps.api.services.chunking_baseline import (
    assess_chunk_quality,
    prepare_structured_for_chunking,
)


def test_pdf_serving_view_revalidates_legacy_headings_without_mutating_source():
    source = StructuredContent(
        document_type="pdf",
        blocks=[
            Block("heading", "ABS树脂 不饱和聚酯树脂", ["旧标题"], level=1, page=1),
            Block("heading", "1 适用范围", ["旧标题", "范围"], level=1, page=1),
            Block("paragraph", "本标准规定相关要求。", ["错误路径"], page=1),
        ],
    )

    prepared, diagnostic = prepare_structured_for_chunking(source)

    assert source.blocks[0].type == "heading"
    assert prepared.blocks[0].type == "paragraph"
    assert prepared.blocks[0].heading_path == []
    assert prepared.blocks[1].type == "heading"
    assert prepared.blocks[1].heading_path == ["1 适用范围"]
    assert prepared.blocks[2].heading_path == ["1 适用范围"]
    assert diagnostic["model_calls"] == 0
    assert diagnostic["headings_demoted"] == 1
    assert diagnostic["headings_kept"] == 1


def test_pdf_numbered_table_row_with_decimal_is_not_a_heading():
    assert native_heading_level("4 丙烯腈 0.5 ABS树脂") is None
    assert native_heading_level("5 ABS树脂 18 聚甲醛树脂") is None
    assert native_heading_level("5.3.4 泄漏的认定") == 3


def test_repeated_pdf_heading_is_demoted_as_likely_running_table_header():
    source = StructuredContent(
        document_type="pdf",
        blocks=[
            Block("heading", "14 双酚 A", ["14 双酚 A"], level=1, page=2),
            Block("paragraph", "第一处表格内容", ["14 双酚 A"], page=2),
            Block("heading", "14 双酚 A", ["14 双酚 A"], level=1, page=4),
            Block("paragraph", "第二处表格内容", ["14 双酚 A"], page=4),
        ],
    )

    prepared, diagnostic = prepare_structured_for_chunking(source)

    assert all(block.type == "paragraph" for block in prepared.blocks)
    assert diagnostic["repeated_headings_demoted"] == 2
    assert diagnostic["headings_kept"] == 0


def test_quality_reports_many_short_chunks_for_review():
    structured = StructuredContent(
        document_type="pdf",
        blocks=[Block("paragraph", "短句", [], page=1)],
    )
    specs = build_chunk_specs(structured)
    quality = assess_chunk_quality(
        structured,
        specs,
        {"policy_version": "test", "mode": "rules", "model_calls": 0},
        minimum_chars=80,
    )

    assert quality["children"] == 1
    assert quality["short_children"] == 1
    assert quality["quality_level"] == "review"
    assert "many_short_chunks" in quality["issues"]


def test_long_pdf_without_reliable_headings_requires_review():
    structured = StructuredContent(
        document_type="pdf",
        blocks=[
            Block("paragraph", f"第 {page} 页正文。" * 20, [], page=page)
            for page in range(1, 7)
        ],
    )
    specs = build_chunk_specs(structured)
    quality = assess_chunk_quality(
        structured,
        specs,
        {
            "policy_version": "test",
            "mode": "normalized_rules",
            "model_calls": 0,
            "headings_total": 5,
            "headings_kept": 0,
            "headings_demoted": 5,
        },
        minimum_chars=80,
    )

    assert quality["quality_level"] == "review"
    assert "no_reliable_headings" in quality["issues"]
