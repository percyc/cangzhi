import re
from .base import BaseParser, Block, StructuredContent, ParserResult


HEADING_RE = re.compile(r'^(#+)\s+(.*)$')
LIST_ITEM_RE = re.compile(r'^\s*([-*+]|\d+\.)\s+(.*)$')


class MarkdownParser(BaseParser):
    def parse(self, content: bytes, content_type: str | None = None) -> ParserResult:
        if not isinstance(content, bytes):
            return ParserResult(success=False, error_message="Markdown 解析器需要文件内容")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return ParserResult(
                success=False,
                error_message="无法解码 Markdown 文件，请将文件保存为 UTF-8 编码后重试",
            )

        text = text.rstrip("\n")
        lines = text.split("\n")
        blocks = []
        heading_path = []
        in_code_block = False
        code_buffer = []

        for paragraph_index, line in enumerate(lines):
            line_stripped = line.strip()

            if line_stripped.startswith("```"):
                if in_code_block:
                    in_code_block = False
                    code_text = "\n".join(code_buffer)
                    blocks.append(Block(
                        type="code_block",
                        text=code_text,
                        heading_path=heading_path.copy(),
                        paragraph_index=paragraph_index,
                    ))
                    code_buffer = []
                else:
                    in_code_block = True
                continue

            if in_code_block:
                code_buffer.append(line)
                continue

            if not line_stripped:
                continue

            heading_match = HEADING_RE.match(line)
            if heading_match:
                hashes, text = heading_match.groups()
                level = len(hashes)
                if level > 6:
                    level = 6
                while len(heading_path) >= level:
                    heading_path.pop()
                heading_path.append(text)
                blocks.append(Block(
                    type="heading",
                    text=text,
                    heading_path=heading_path.copy(),
                    level=level,
                    paragraph_index=paragraph_index,
                ))
                continue

            list_match = LIST_ITEM_RE.match(line)
            if list_match:
                item_text = list_match.group(2)
                blocks.append(Block(
                    type="list_item",
                    text=item_text,
                    heading_path=heading_path.copy(),
                    paragraph_index=paragraph_index,
                ))
                continue

            if line_stripped.startswith("> "):
                quote_text = line_stripped[2:]
                blocks.append(Block(
                    type="blockquote",
                    text=quote_text,
                    heading_path=heading_path.copy(),
                    paragraph_index=paragraph_index,
                ))
                continue

            blocks.append(Block(
                type="paragraph",
                text=line,
                heading_path=heading_path.copy(),
                paragraph_index=paragraph_index,
            ))

        if code_buffer:
            blocks.append(Block(
                type="code_block",
                text="\n".join(code_buffer),
                heading_path=heading_path.copy(),
            ))

        structured_content = StructuredContent(
            document_type="markdown",
            blocks=blocks,
        )

        return ParserResult(
            success=True,
            structured_content=structured_content,
        )

    def can_parse(self, content_type: str | None, file_name: str | None = None) -> bool:
        if content_type and ("text/markdown" in content_type.lower() or "markdown" in content_type.lower()):
            return True
        if file_name and file_name.lower().endswith((".md", ".markdown")):
            return True
        return False
