from io import BytesIO
from .base import BaseParser, Block, StructuredContent, ParserResult


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

                style_name = para.style.name.lower() if para.style else ""

                if style_name.startswith("heading") or style_name == "title":
                    if style_name.startswith("heading"):
                        try:
                            level = int(style_name[-1])
                            if level > 6:
                                level = 6
                        except ValueError:
                            level = 1
                    else:
                        level = 1

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
