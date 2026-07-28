from io import BytesIO
from .base import BaseParser, Block, StructuredContent, ParserResult


class PdfParser(BaseParser):
    def parse(self, content: bytes, content_type: str | None = None) -> ParserResult:
        try:
            import pypdf
        except ImportError:
            return ParserResult(
                success=False,
                error_message="PDF 解析依赖 pypdf 库，请安装 pypdf>=3.0",
                error_details={"import_error": "pypdf not found"},
            )

        try:
            blocks = []
            heading_path = []
            reader = pypdf.PdfReader(BytesIO(content))

            for page_idx, page in enumerate(reader.pages):
                page_number = page_idx + 1
                text = page.extract_text()
                if not text:
                    continue

                lines = text.split("\n")
                paragraph_index = 0

                for line in lines:
                    line = line.strip()
                    if not line:
                        continue

                    is_heading = (
                        (len(line) < 100 and line.isupper())
                        or line.startswith(("Chapter", "Section"))
                    )
                    if is_heading:
                        level = 1 if len(line) < 30 else 2
                        while len(heading_path) >= level:
                            heading_path.pop()
                        heading_path.append(line)
                        blocks.append(Block(
                            type="heading",
                            text=line,
                            heading_path=heading_path.copy(),
                            page=page_number,
                            level=level,
                            paragraph_index=paragraph_index,
                        ))
                    else:
                        blocks.append(Block(
                            type="paragraph",
                            text=line,
                            heading_path=heading_path.copy(),
                            page=page_number,
                            paragraph_index=paragraph_index,
                        ))
                    paragraph_index += 1

            structured_content = StructuredContent(
                document_type="pdf",
                blocks=blocks,
                metadata={
                    "page_count": len(reader.pages),
                },
            )

            return ParserResult(
                success=True,
                structured_content=structured_content,
            )

        except Exception as e:
            return ParserResult(
                success=False,
                error_message=f"PDF 解析失败: {str(e)}",
                error_details={"exception": str(e)},
            )

    def can_parse(self, content_type: str | None, file_name: str | None = None) -> bool:
        if content_type and "application/pdf" in content_type.lower():
            return True
        if file_name and file_name.lower().endswith(".pdf"):
            return True
        return False
