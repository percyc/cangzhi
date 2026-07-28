from .base import BaseParser, Block, StructuredContent, ParserResult, DocumentType


class NoteParser(BaseParser):
    def parse(self, content: bytes | str, content_type: str | None = None) -> ParserResult:
        if isinstance(content, bytes):
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                return ParserResult(
                    success=False,
                    error_message="无法解码笔记内容，请检查编码格式",
                )
        else:
            text = content

        text = text.strip()
        if not text:
            return ParserResult(
                success=True,
                structured_content=StructuredContent(
                    document_type="note",
                    blocks=[],
                ),
            )

        lines = text.split("\n")
        blocks = []
        heading_path = []

        for paragraph_index, line in enumerate(lines):
            line_stripped = line.strip()
            if not line_stripped:
                continue

            if line.startswith("#"):
                level = len(line) - len(line.lstrip("#"))
                if level > 6:
                    level = 6
                heading_text = line_stripped[level:].strip()
                while len(heading_path) >= level:
                    heading_path.pop()
                heading_path.append(heading_text)
                blocks.append(Block(
                    type="heading",
                    text=heading_text,
                    heading_path=heading_path.copy(),
                    level=level,
                    paragraph_index=paragraph_index,
                ))
            else:
                blocks.append(Block(
                    type="paragraph",
                    text=line,
                    heading_path=heading_path.copy(),
                    paragraph_index=paragraph_index,
                ))

        structured_content = StructuredContent(
            document_type="note",
            blocks=blocks,
        )

        return ParserResult(
            success=True,
            structured_content=structured_content,
        )

    def can_parse(self, content_type: str | None, file_name: str | None = None) -> bool:
        return content_type == "application/x-note"
