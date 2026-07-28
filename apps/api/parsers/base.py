from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Literal

BlockType = Literal[
    "heading",
    "paragraph",
    "list_item",
    "table",
    "code_block",
    "blockquote",
]
DocumentType = Literal["note", "txt", "markdown", "pdf", "docx"]


@dataclass
class Block:
    type: BlockType
    text: str
    heading_path: list[str]
    page: int | None = None
    paragraph_index: int | None = None
    level: int | None = None

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "text": self.text,
            "heading_path": self.heading_path,
            "page": self.page,
            "paragraph_index": self.paragraph_index,
            "level": self.level,
        }


@dataclass
class StructuredContent:
    document_type: DocumentType
    blocks: list[Block]
    schema_version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "document_type": self.document_type,
            "blocks": [b.to_dict() for b in self.blocks],
            "metadata": self.metadata,
        }

    def full_text(self) -> str:
        return "\n".join(b.text for b in self.blocks if b.text and b.text.strip())


@dataclass
class ParserResult:
    success: bool
    structured_content: StructuredContent | None = None
    error_message: str | None = None
    error_details: dict[str, Any] | None = None

class BaseParser(abc.ABC):
    @abc.abstractmethod
    def parse(self, content: bytes | str, content_type: str | None = None) -> ParserResult:
        pass

    def can_parse(self, content_type: str | None, file_name: str | None = None) -> bool:
        return False


def get_parser_for_content(
    content_type: str | None,
    file_name: str | None = None,
) -> BaseParser:
    from .note import NoteParser
    from .text import TextParser
    from .markdown import MarkdownParser
    from .pdf import PdfParser
    from .docx import DocxParser

    content_type = (content_type or "").lower()

    # Check specific extensions first
    if file_name:
        lower_name = file_name.lower()
        if lower_name.endswith((".md", ".markdown")):
            return MarkdownParser()
        if lower_name.endswith(".pdf"):
            return PdfParser()
        if lower_name.endswith(".docx"):
            return DocxParser()
        if lower_name.endswith(".txt"):
            return TextParser()

    if content_type == "application/x-note":
        return NoteParser()
    if "text/markdown" in content_type or "markdown" in content_type:
        return MarkdownParser()
    if "application/pdf" in content_type:
        return PdfParser()
    if (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        in content_type
    ):
        return DocxParser()
    if "text/plain" in content_type:
        return TextParser()

    return TextParser()
