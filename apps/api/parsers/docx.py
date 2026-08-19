import re
from io import BytesIO

from .base import BaseParser, Block, ParserResult, StructuredContent

_HEADING_STYLE_PATTERN = re.compile(
    r"(?:heading|titlelevel|标题)[\s_-]*(\d+)$", re.IGNORECASE
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
    def parse(self, content: bytes, content_type: str | None = None) -> ParserResult:
        try:
            from docx import Document
        except ImportError:
            return ParserResult(
                success=False,
                error_message="DOCX 解析依赖 python-docx 库，请安装 python-docx>=0.8",
                error_details={"import_error": "python-docx not found"},
            )

        try:
            blocks = []
            heading_path = []
            doc = Document(BytesIO(content))
            paragraph_index = 0
            for para_idx, para in enumerate(doc.paragraphs):
                text = para.text.strip()
                if not text:
                    continue

                style_name = _safe_style_name(para)
                level = _heading_level(style_name)

                if level is not None:
                    while len(heading_path) >= level:
                        heading_path.pop()
                    heading_path.append(text)

                    blocks.append(Block(
                        type="heading",
                        text=text,
                        heading_path=heading_path.copy(),
                        level=level,
                        paragraph_index=para_idx,
                    ))
                else:
                    blocks.append(Block(
                        type="paragraph",
                        text=para.text,
                        heading_path=heading_path.copy(),
                        paragraph_index=para_idx,
                    ))
                paragraph_index += 1

            for table in doc.tables:
                lines = []
                for row in table.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    lines.append("\t".join(cells))
                table_text = "\n".join(lines)
                blocks.append(Block(
                    type="table",
                    text=table_text,
                    heading_path=heading_path.copy(),
                    paragraph_index=paragraph_index,
                ))
                paragraph_index += 1

            structured_content = StructuredContent(
                document_type="docx",
                blocks=blocks,
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
