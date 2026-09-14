import re
from io import BytesIO

from .base import BaseParser, Block, ParserResult, StructuredContent

_HEADING_STYLE_PATTERN = re.compile(
    r"(?:heading|titlelevel|标题)[\s_-]*(\d+)$", re.IGNORECASE
)

_DOCX_PARSER_VERSION = "1"
_DOCX_INDEX_SEMANTICS = (
    "paragraph_index 与 Block.extra.docx_body_index 相同，记录块在 DOCX 正文 "
    "(w:body) 直接子节点中按出现顺序的零基位置；仅统计 w:p 与 w:tbl 节点，"
    "w:sectPr 不计数。空白段落计入索引但不输出块。嵌套表格与修订包装元素 "
    "(w:sdt / w:ins / w:del / w:customXml 等) 当前不展开，复杂结构请回到原文核对。"
)


def _safe_style_name(paragraph) -> str:
    """Read a paragraph style without rejecting vendor-specific DOCX styles."""

    try:
        style = paragraph.style
        return style.name.strip().lower() if style and style.name else ""
    except (KeyError, ValueError):
        # Some producers write a non-standard value into w:style/@w:type.
        # python-docx rejects that enum while the paragraph text remains valid.
        style_id = paragraph._p.style
        return style_id.strip().lower() if isinstance(style_id, str) else ""


def _heading_level(style_name: str) -> int | None:
    if style_name == "title":
        return 1
    match = _HEADING_STYLE_PATTERN.search(style_name)
    if match is None:
        return None
    return min(max(int(match.group(1)), 1), 6)


class DocxParser(BaseParser):
    """Parse DOCX bodies preserving the original paragraph/table order.

    CZ-Q06: paragraphs and tables are emitted in the order they appear
    among the document body's direct ``w:p``/``w:tbl`` children so
    heading attribution, parent-section grouping and table
    interleaving all match the source. Each emitted block records its
    zero-based body position both as ``paragraph_index`` and under
    ``Block.extra.docx_body_index``; the trailing ``w:sectPr`` is not
    counted and blank paragraphs are counted but never emitted.

    The parser walks only direct body children. Nested tables and
    revision wrappers (``w:sdt``, ``w:ins``, ``w:del``,
    ``w:customXml`` ...) are intentionally not expanded; complex
    structures remain readable in the original file but their inner
    blocks are not turned into separate ``Block`` records. Full
    table metadata is deferred to a later CZ-N step.
    """

    def parse(self, content: bytes, content_type: str | None = None) -> ParserResult:
        try:
            from docx import Document
            from docx.oxml.ns import qn
            from docx.table import Table
            from docx.text.paragraph import Paragraph
        except ImportError:
            return ParserResult(
                success=False,
                error_message="DOCX 解析依赖 python-docx 库，请安装 python-docx>=0.8",
                error_details={"import_error": "python-docx not found"},
            )

        try:
            blocks: list[Block] = []
            heading_stack: list[tuple[int, str]] = []
            doc = Document(BytesIO(content))
            w_p = qn("w:p")
            w_tbl = qn("w:tbl")
            body_index = 0

            for child in doc.element.body.iterchildren():
                tag = child.tag
                if tag == w_p:
                    current_index = body_index
                    body_index += 1
                    para = Paragraph(child, doc)
                    text = para.text.strip()
                    if not text:
                        # Blank paragraphs still occupy a body index slot
                        # so subsequent blocks keep the right offset, but
                        # they are not emitted to the consumer.
                        continue

                    style_name = _safe_style_name(para)
                    level = _heading_level(style_name)

                    if level is not None:
                        while heading_stack and heading_stack[-1][0] >= level:
                            heading_stack.pop()
                        heading_stack.append((level, text))
                        heading_path = [title for _, title in heading_stack]

                        blocks.append(Block(
                            type="heading",
                            text=text,
                            heading_path=heading_path,
                            level=level,
                            paragraph_index=current_index,
                            extra={"docx_body_index": current_index},
                        ))
                    else:
                        heading_path = [title for _, title in heading_stack]
                        blocks.append(Block(
                            type="paragraph",
                            text=para.text,
                            heading_path=heading_path,
                            paragraph_index=current_index,
                            extra={"docx_body_index": current_index},
                        ))
                elif tag == w_tbl:
                    current_index = body_index
                    body_index += 1
                    table = Table(child, doc)
                    lines = []
                    for row in table.rows:
                        cells = [cell.text.strip() for cell in row.cells]
                        lines.append("\t".join(cells))
                    table_text = "\n".join(lines)
                    heading_path = [title for _, title in heading_stack]
                    blocks.append(Block(
                        type="table",
                        text=table_text,
                        heading_path=heading_path,
                        paragraph_index=current_index,
                        extra={"docx_body_index": current_index},
                    ))
                # w:sectPr, w:sdt, w:ins, w:del, w:customXml and other
                # body wrappers are intentionally skipped without
                # contributing to body_index. See the class docstring.

            structured_content = StructuredContent(
                document_type="docx",
                blocks=blocks,
                metadata={
                    "docx_extraction": {
                        "parser_version": _DOCX_PARSER_VERSION,
                        "index_semantics": _DOCX_INDEX_SEMANTICS,
                        "nested_tables_supported": False,
                        "revision_tracking_supported": False,
                        "body_indexed_children": body_index,
                    },
                },
            )

            return ParserResult(
                success=True,
                structured_content=structured_content,
            )

        except Exception as e:
            return ParserResult(
                success=False,
                error_message=f"DOCX 解析失败: {str(e)}",
                error_details={"exception": str(e)},
            )

    def can_parse(self, content_type: str | None, file_name: str | None = None) -> bool:
        docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if content_type and docx_mime in content_type.lower():
            return True
        if file_name and file_name.lower().endswith(".docx"):
            return True
        return False
