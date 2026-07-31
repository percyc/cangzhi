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
DocumentType = Literal[
    "note",
    "txt",
    "markdown",
    "pdf",
    "doc",
    "docx",
    "xlsx",
    "xls",
    "html",
]


@dataclass
class Block:
    type: BlockType
    text: str
    heading_path: list[str]
    page: int | None = None
    paragraph_index: int | None = None
    level: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "text": self.text,
            "heading_path": self.heading_path,
            "page": self.page,
            "paragraph_index": self.paragraph_index,
            "level": self.level,
            "extra": dict(self.extra),
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
    from .doc import DocParser
    from .docx import DocxParser
    from .html import HtmlParser
    from .markdown import MarkdownParser
    from .note import NoteParser
    from .pdf import PdfParser
    from .spreadsheet import XlsParser, XlsxParser
    from .text import TextParser

    content_type = (content_type or "").lower()

    # Check specific extensions first
    if file_name:
        lower_name = file_name.lower()
        if lower_name.endswith((".md", ".markdown")):
            return MarkdownParser()
        if lower_name.endswith(".pdf"):
            return PdfParser()
        if lower_name.endswith(".doc"):
            return DocParser()
        if lower_name.endswith(".docx"):
            return DocxParser()
        if lower_name.endswith(".xlsx"):
            return XlsxParser()
        if lower_name.endswith(".xls"):
            return XlsParser()
        if lower_name.endswith((".html", ".htm")):
            return HtmlParser()
        if lower_name.endswith(".txt"):
            return TextParser()

    if content_type == "application/x-note":
        return NoteParser()
    if "text/markdown" in content_type or "markdown" in content_type:
        return MarkdownParser()
    if "application/pdf" in content_type:
        return PdfParser()
    if content_type in {
        "application/msword",
        "application/doc",
        "application/vnd.ms-word",
        "application/vnd.msword",
        "application/winword",
    }:
        return DocParser()
    if (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        in content_type
    ):
        return DocxParser()
    if (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        in content_type
    ):
        return XlsxParser()
    if content_type in {
        "application/vnd.ms-excel",
        "application/excel",
        "application/x-excel",
        "application/x-msexcel",
    }:
        return XlsParser()
    if "html" in content_type:
        return HtmlParser()
    if "text/plain" in content_type:
        return TextParser()

    return TextParser()
