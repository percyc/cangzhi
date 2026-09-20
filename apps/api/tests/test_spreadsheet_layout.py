from io import BytesIO

from openpyxl import Workbook

from apps.api.parsers.spreadsheet import XlsxParser
from apps.api.services.chunker import build_chunk_specs
from apps.api.services.structured_table import extract_table_row_payloads


def parse_rows(rows, merges=()):
    workbook = Workbook()
    for row in rows:
        workbook.active.append(row)
    for span in merges:
        workbook.active.merge_cells(span)
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    result = XlsxParser().parse(output.getvalue())
    assert result.success, result.error_message
    return result.structured_content


def test_regular_region_and_notes_coexist_without_losing_evidence():
    content = parse_rows([
        ["名称", "数量"], ["物品甲", 2], [],
        ["附注：本表只记录已验收物品"],
    ])
    rows = extract_table_row_payloads(content)
    assert len(rows) == 1
    assert rows[0]["row_number"] == 2
    assert "附注：本表只记录已验收物品" in content.full_text()
    specs = build_chunk_specs(content)
    assert any(s.chunk_type == "dataset_catalog" for s in specs)
    assert not any("附注：本表只记录已验收物品" in s.content for s in specs)
    assert len({s.external_id for s in specs}) == len(specs)
    for role in ("parent", "child"):
        indices = [s.order_index for s in specs if s.role == role]
        assert len(set(indices)) == len(indices)


def test_merged_multilevel_headers_are_preserved_without_forward_fill():
    content = parse_rows([
        ["项目", "本期", None], [None, "数量", "金额"],
        ["物品乙", 3, 50],
    ], ("B1:C1", "A1:A2"))
    assert extract_table_row_payloads(content) == []
    text = content.full_text()
    assert "本期" in text and "数量" in text and "金额" in text
    assert "行 1｜A列=项目｜B列=本期" in text
    assert "行 2｜B列=数量｜C列=金额" in text
    fallback = [b for b in content.blocks if b.extra.get("dataset_eligible") is False]
    assert fallback and "merged_cells" in fallback[0].extra["layout_risks"]


def test_side_by_side_tables_are_not_cross_joined():
    content = parse_rows([["名称", "数量", None, "名称", "数量"], ["甲", 1, None, "乙", 9]])
    assert extract_table_row_payloads(content) == []
    assert "D列=乙" in content.full_text()
    assert any("separated_columns" in b.extra.get("layout_risks", []) for b in content.blocks)


def test_summary_rows_are_not_counted_as_detail():
    content = parse_rows([["名称", "数量"], ["甲", 2], ["乙", 3], ["合计", 5]])
    assert extract_table_row_payloads(content) == []
    assert "合计" in content.full_text()
    assert build_chunk_specs(content) == []


def test_regular_table_with_total_column_still_supports_exact_queries():
    content = parse_rows([["名称", "数量", "合计"], ["甲", 2, 5], ["乙", 3, 8]])
    assert len(extract_table_row_payloads(content)) == 2


def test_no_header_data_is_not_discarded_or_claimed_to_be_relational():
    content = parse_rows([["无标题说明"], ["第二行说明"]])
    assert extract_table_row_payloads(content) == []
    assert "无标题说明" in content.full_text()
    assert "第二行说明" in content.full_text()
