from datetime import datetime
from io import BytesIO

from apps.api.parsers import (
    DocParser,
    DocxParser,
    MarkdownParser,
    NoteParser,
    PdfParser,
    TextParser,
    XlsParser,
    XlsxParser,
    get_parser_for_content,
)


class TestNoteParser:
    def test_parse_empty_note(self):
        parser = NoteParser()
        result = parser.parse("")
        assert result.success is True
        assert result.structured_content is not None
        assert len(result.structured_content.blocks) == 0

    def test_parse_simple_note(self):
        parser = NoteParser()
        text = """# Title
## Section
This is a paragraph.
Another paragraph.
"""
        result = parser.parse(text)
        assert result.success is True
        blocks = result.structured_content.blocks
        assert len(blocks) == 4
        assert blocks[0].type == "heading"
        assert blocks[0].level == 1
        assert blocks[0].text == "Title"
        assert blocks[1].type == "heading"
        assert blocks[1].level == 2
        assert blocks[1].text == "Section"

    def test_parse_note_full_text(self):
        parser = NoteParser()
        text = "Hello\nWorld"
        result = parser.parse(text)
        assert result.success is True
        full_text = result.structured_content.full_text()
        assert full_text == "Hello\nWorld"


class TestTextParser:
    def test_decode_utf8(self):
        parser = TextParser()
        content = "Hello 中文".encode("utf-8")
        result = parser.parse(content)
        assert result.success is True
        assert len(result.structured_content.blocks) == 1

    def test_decode_utf8_bom(self):
        parser = TextParser()
        content = b'\xef\xbb\xbfHello'
        result = parser.parse(content)
        assert result.success is True
        assert result.structured_content.blocks[0].text == "Hello"

    def test_decode_error_handling(self):
        parser = TextParser()
        content = b'\xff\xfe\xfd'
        result = parser.parse(content)
        assert result.success is False
        assert "无法解码" in result.error_message


class TestMarkdownParser:
    def test_parse_headings(self):
        parser = MarkdownParser()
        content = """# Heading 1
## Heading 2
### Heading 3
""".encode("utf-8")
        result = parser.parse(content)
        assert result.success is True
        blocks = result.structured_content.blocks
        assert len(blocks) == 3
        assert blocks[0].level == 1
        assert blocks[1].level == 2
        assert blocks[2].level == 3

    def test_parse_code_block(self):
        parser = MarkdownParser()
        content = """```python
def hello():
    print("hello")
```
""".encode("utf-8")
        result = parser.parse(content)
        assert result.success is True
        blocks = result.structured_content.blocks
        assert len(blocks) == 1
        assert blocks[0].type == "code_block"
        assert "def hello" in blocks[0].text

    def test_parse_list_items(self):
        parser = MarkdownParser()
        content = """- Item 1
- Item 2
1. Item 3
""".encode("utf-8")
        result = parser.parse(content)
        assert result.success is True
        blocks = result.structured_content.blocks
        assert len(blocks) == 3
        assert all(b.type == "list_item" for b in blocks)

    def test_get_parser_for_markdown_by_extension(self):
        parser = get_parser_for_content("text/plain", "test.md")
        assert isinstance(parser, MarkdownParser)


class TestStructuredContent:
    def test_to_dict_has_correct_schema(self):
        from apps.api.parsers.base import StructuredContent, Block
        blocks = [Block(type="paragraph", text="test", heading_path=[])]
        sc = StructuredContent(document_type="txt", blocks=blocks)
        data = sc.to_dict()
        assert data["schema_version"] == 1
        assert "document_type" in data
        assert len(data["blocks"]) == 1
        assert "heading_path" in data["blocks"][0]


class TestOfficeParsers:
    def test_selects_excel_parsers_by_extension_and_mime(self):
        assert isinstance(
            get_parser_for_content("application/octet-stream", "ledger.xlsx"),
            XlsxParser,
        )
        assert isinstance(
            get_parser_for_content(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                None,
            ),
            XlsxParser,
        )
        assert isinstance(
            get_parser_for_content("application/vnd.ms-excel", "ledger.xls"),
            XlsParser,
        )

    def test_parse_xlsx_preserves_sheets_rows_empty_cells_and_formula(self):
        from openpyxl import Workbook

        workbook = Workbook()
        summary = workbook.active
        summary.title = "汇总"
        summary.append(["项目", "一月", "二月", "合计"])
        summary.append(["收入", 100, None, "=SUM(B2:C2)"])
        summary.append([None, None, None, None])
        details = workbook.create_sheet("明细")
        details.append(["日期", "说明"])
        details.append([datetime(2026, 7, 31, 8, 30), "会员收入"])
        source = BytesIO()
        workbook.save(source)

        result = XlsxParser().parse(source.getvalue())

        assert result.success is True
        structured = result.structured_content
        assert structured.document_type == "xlsx"
        assert structured.metadata == {
            "source_format": "xlsx",
            "sheet_count": 2,
            "sheet_names": ["汇总", "明细"],
            "non_empty_rows": 4,
            "non_empty_cells": 11,
            "formula_cells": 1,
        }
        assert [block.type for block in structured.blocks] == [
            "heading",
            "table",
            "heading",
            "table",
        ]
        assert structured.blocks[1].heading_path == ["汇总"]
        assert "收入\t100\t\t=SUM(B2:C2)" in structured.blocks[1].text
        assert "2026-07-31 08:30:00\t会员收入" in structured.blocks[3].text

    def test_parse_blank_xlsx_returns_no_fake_content(self):
        from openpyxl import Workbook

        workbook = Workbook()
        source = BytesIO()
        workbook.save(source)

        result = XlsxParser().parse(source.getvalue())

        assert result.success is True
        assert result.structured_content.blocks == []
        assert result.structured_content.metadata["sheet_names"] == ["Sheet"]

    def test_selects_legacy_doc_parser_by_extension_and_mime(self):
        assert isinstance(
            get_parser_for_content("application/octet-stream", "legacy.doc"),
            DocParser,
        )
        assert isinstance(
            get_parser_for_content("application/msword", None),
            DocParser,
        )

    def test_doc_parser_reports_missing_converter(self, monkeypatch):
        monkeypatch.setattr(
            "apps.api.parsers.doc.shutil.which",
            lambda _name: None,
        )

        result = DocParser().parse(b"legacy word data")

        assert result.success is False
        assert result.error_details == {"reason": "doc_converter_unavailable"}

    def test_doc_parser_converts_then_preserves_structure(self, monkeypatch):
        from types import SimpleNamespace
        from docx import Document

        def fake_run(arguments, **_kwargs):
            output_directory = arguments[arguments.index("--outdir") + 1]
            converted = Document()
            converted.add_heading("旧版合同", level=1)
            converted.add_paragraph("转换后的正文")
            converted.save(f"{output_directory}/source.docx")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(
            "apps.api.parsers.doc.shutil.which",
            lambda _name: "/usr/bin/soffice",
        )
        monkeypatch.setattr(
            "apps.api.parsers.doc.subprocess.run",
            fake_run,
        )

        result = DocParser().parse(b"legacy word data")

        assert result.success is True
        assert result.structured_content.document_type == "doc"
        assert result.structured_content.full_text() == "旧版合同\n转换后的正文"
        assert result.structured_content.metadata == {
            "source_format": "doc",
            "converted_format": "docx",
        }

    def test_parse_docx_heading_paragraph_and_table(self):
        from docx import Document

        document = Document()
        document.add_heading("合同条款", level=1)
        document.add_paragraph("这是正文。")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "甲方"
        table.cell(0, 1).text = "乙方"
        source = BytesIO()
        document.save(source)

        result = DocxParser().parse(source.getvalue())
        assert result.success is True
        assert [block.type for block in result.structured_content.blocks] == [
            "heading",
            "paragraph",
            "table",
        ]
        assert result.structured_content.blocks[1].heading_path == ["合同条款"]
        assert result.structured_content.blocks[2].text == "甲方\t乙方"

    def test_parse_pdf_preserves_page_count(self):
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        source = BytesIO()
        writer.write(source)

        result = PdfParser().parse(source.getvalue())
        assert result.success is True
        assert result.structured_content.metadata["page_count"] == 1
