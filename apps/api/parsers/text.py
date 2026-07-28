from .base import BaseParser, Block, StructuredContent, ParserResult


class TextParser(BaseParser):
    def parse(self, content: bytes, content_type: str | None = None) -> ParserResult:
        if not isinstance(content, bytes):
            return ParserResult(success=False, error_message="文本解析器需要文件内容")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return ParserResult(
                success=False,
                error_message="无法解码文本文件，请将文件保存为 UTF-8 编码后重试",
            )

        text = text.rstrip("\n")
        lines = text.split("\n")
        blocks = []
        heading_path = []

        for paragraph_index, line in enumerate(lines):
            line = line.rstrip("\r")
            if not line.strip():
                continue

            blocks.append(Block(
                type="paragraph",
                text=line,
                heading_path=heading_path.copy(),
                paragraph_index=paragraph_index,
            ))

        structured_content = StructuredContent(
            document_type="txt",
            blocks=blocks,
        )

        return ParserResult(
            success=True,
            structured_content=structured_content,
        )

    def can_parse(self, content_type: str | None, file_name: str | None = None) -> bool:
        if content_type and "text/plain" in content_type.lower():
            return True
        if file_name and file_name.lower().endswith(".txt"):
            return True
        return False
