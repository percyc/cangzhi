from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import urljoin

from .base import BaseParser, Block, ParserResult, StructuredContent


_REMOVE_TAGS = {
    "script",
    "style",
    "noscript",
    "iframe",
    "svg",
    "canvas",
    "template",
    "form",
    "input",
    "button",
    "object",
    "embed",
    "audio",
    "video",
    "source",
    "track",
    "nav",
    "footer",
    "header",
}

_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "aside",
    "ul",
    "ol",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "pre",
    "table",
    "figure",
    "figcaption",
    "details",
    "summary",
    "hr",
    "br",
}

_HEADING_TAGS = {f"h{i}" for i in range(1, 7)}

_LANG_ATTR_RE = re.compile(r'lang\s*=\s*"([^"]+)"', re.IGNORECASE)
_META_NAME_RE = re.compile(
    r"""<meta\s+[^>]*?name\s*=\s*["'](?P<name>[^"']+)["'][^>]*?content\s*=\s*["'](?P<content>[^"']*)["'][^>]*>""",
    re.IGNORECASE,
)
_META_PROPERTY_RE = re.compile(
    r"""<meta\s+[^>]*?property\s*=\s*["'](?P<name>[^"']+)["'][^>]*?content\s*=\s*["'](?P<content>[^"']*)["'][^>]*>""",
    re.IGNORECASE,
)
_META_CONTENT_FIRST_RE = re.compile(
    r"""<meta\s+[^>]*?content\s*=\s*["'](?P<content>[^"']*)["'][^>]*?(?:name|property)\s*=\s*["'](?P<name>[^"']+)["'][^>]*>""",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title[^>]*>(?P<title>.*?)</title>", re.IGNORECASE | re.DOTALL)
_LINK_RE = re.compile(
    r"""<link\s+[^>]*?rel\s*=\s*["'][^"']*canonical[^"']*["'][^>]*?href\s*=\s*["'](?P<href>[^"']+)["'][^>]*>""",
    re.IGNORECASE,
)
_LANG_HTML_RE = re.compile(r"<html[^>]*>", re.IGNORECASE)


def _decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _strip_tags(html: str) -> str:
    """Remove HTML tags while keeping visible text.

    ``HTMLParser`` is used so the output is unaffected by malformed tags,
    embedded scripts, or unusual encodings.
    """

    class _Stripper(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self._skip_depth = 0
            self._text: list[str] = []

        def handle_starttag(self, tag: str, attrs):  # type: ignore[override]
            lowered = tag.lower()
            if lowered in _REMOVE_TAGS:
                self._skip_depth += 1
                return
            if lowered == "br":
                self._text.append("\n")
                return
            if lowered in _BLOCK_TAGS:
                self._text.append("\n")
                return

        def handle_endtag(self, tag: str):  # type: ignore[override]
            lowered = tag.lower()
            if lowered in _REMOVE_TAGS and self._skip_depth > 0:
                self._skip_depth -= 1
                return
            if lowered in _BLOCK_TAGS:
                self._text.append("\n")

        def handle_data(self, data: str):  # type: ignore[override]
            if self._skip_depth == 0:
                self._text.append(data)

    stripper = _Stripper()
    stripper.feed(html)
    stripper.close()
    text = "".join(stripper._text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_meta(html: str, names: Iterable[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    targets = {n.lower() for n in names}
    for regex in (_META_NAME_RE, _META_PROPERTY_RE, _META_CONTENT_FIRST_RE):
        for match in regex.finditer(html):
            name = match.group("name").lower()
            content = match.group("content").strip()
            if name in targets and content and name not in found:
                found[name] = content
    return found


def _extract_title(html: str) -> str | None:
    match = _TITLE_RE.search(html)
    if match is None:
        return None
    title = _strip_tags(match.group("title"))
    return title.strip() or None


def _extract_canonical(html: str) -> str | None:
    match = _LINK_RE.search(html)
    if match is None:
        return None
    return match.group("href").strip() or None


def _detect_language(html: str) -> str | None:
    match = _LANG_HTML_RE.search(html)
    if match is None:
        return None
    inner = _LANG_ATTR_RE.search(match.group(0))
    if inner is None:
        return None
    return inner.group(1).strip() or None


class HtmlParser(BaseParser):
    """Light-weight HTML parser that produces :class:`StructuredContent`.

    No JavaScript is executed. Only inline content is used. The parser
    converts headings and paragraphs into blocks; nested menus, ads and
    non-content sections are skipped.
    """

    def parse(self, content: bytes | str, content_type: str | None = None) -> ParserResult:
        if isinstance(content, bytes):
            html = _decode_bytes(content)
        else:
            html = content
        if not html or not html.strip():
            return ParserResult(
                success=False,
                error_message="HTML 内容为空",
                error_details={"reason": "empty_body"},
            )

        try:
            metadata = self._extract_metadata(html)
            blocks, plain_text = self._extract_blocks(html)
        except Exception as exc:  # noqa: BLE001 — defensive: malformed HTML
            return ParserResult(
                success=False,
                error_message=f"HTML 解析失败：{exc}",
                error_details={"exception": str(exc)},
            )

        metadata["raw_text"] = plain_text
        metadata["char_count"] = len(plain_text)

        structured = StructuredContent(
            document_type="html",
            blocks=blocks,
            metadata=metadata,
        )
        return ParserResult(success=True, structured_content=structured)

    def can_parse(self, content_type: str | None, file_name: str | None = None) -> bool:
        if content_type and "html" in content_type.lower():
            return True
        if file_name and file_name.lower().endswith((".html", ".htm")):
            return True
        return False

    def _extract_metadata(self, html: str) -> dict[str, Any]:
        meta: dict[str, Any] = {}
        title = _extract_title(html)
        if title:
            meta["title"] = title
        meta.update(
            _extract_meta(
                html,
                [
                    "description",
                    "author",
                    "keywords",
                    "pubdate",
                    "article:published",
                ],
            )
        )

        # OpenGraph / Twitter fallbacks.
        og = _extract_meta(
            html,
            [
                "og:title",
                "og:description",
                "og:image",
                "og:url",
                "og:site_name",
                "article:published_time",
                "article:author",
                "twitter:title",
                "twitter:description",
            ],
        )
        for key, value in og.items():
            meta.setdefault(key, value)
        if "title" not in meta:
            fallback_title = meta.get("og:title") or meta.get("twitter:title")
            if fallback_title:
                meta["title"] = fallback_title
        if "description" not in meta:
            fallback_description = meta.get("og:description") or meta.get(
                "twitter:description"
            )
            if fallback_description:
                meta["description"] = fallback_description
        if "author" not in meta and meta.get("article:author"):
            meta["author"] = meta["article:author"]

        published = meta.get("article:published_time")
        if not published:
            # Try the generic published_time variant.
            published = meta.get("article:published") or meta.get("pubdate")
        if published:
            meta["published_at"] = published

        canonical = _extract_canonical(html)
        if canonical:
            meta["canonical_url"] = canonical
        language = _detect_language(html)
        if language:
            meta["language"] = language
        return meta

    def _extract_blocks(self, html: str) -> tuple[list[Block], str]:
        class _Walker(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self._blocks: list[Block] = []
                self._heading_path: list[str] = []
                self._skip_depth = 0
                self._buffer: list[str] = []
                self._current_type: str | None = None
                self._current_level: int | None = None
                self._paragraph_index = 0
                self._list_index: list[int] = []

            def _flush(self) -> None:
                if self._current_type is None:
                    return
                text = "".join(self._buffer).strip()
                if text:
                    self._blocks.append(
                        Block(
                            type=self._current_type,
                            text=text,
                            heading_path=self._heading_path.copy(),
                            level=self._current_level,
                            paragraph_index=self._paragraph_index,
                        )
                    )
                    self._paragraph_index += 1
                self._buffer = []
                self._current_type = None
                self._current_level = None

            def handle_starttag(self, tag: str, attrs):  # type: ignore[override]
                lowered = tag.lower()
                if lowered in _REMOVE_TAGS:
                    self._skip_depth += 1
                    return
                if lowered in _HEADING_TAGS:
                    self._flush()
                    self._current_type = "heading"
                    self._current_level = int(lowered[1])
                    return
                if lowered == "p":
                    self._flush()
                    self._current_type = "paragraph"
                    return
                if lowered == "li":
                    self._flush()
                    self._current_type = "list_item"
                    return
                if lowered == "blockquote":
                    self._flush()
                    self._current_type = "blockquote"
                    return
                if lowered == "pre":
                    self._flush()
                    self._current_type = "code_block"
                    return
                if lowered in {"div", "section", "article", "main"}:
                    if self._current_type is not None:
                        self._buffer.append("\n")
                    return
                if lowered == "br":
                    self._buffer.append("\n")

            def handle_endtag(self, tag: str):  # type: ignore[override]
                lowered = tag.lower()
                if lowered in _REMOVE_TAGS and self._skip_depth > 0:
                    self._skip_depth -= 1
                    return
                if lowered in _HEADING_TAGS or lowered in {"p", "li", "blockquote", "pre"}:
                    self._flush()
                    return
                if lowered in {"div", "section", "article", "main"}:
                    if self._current_type is not None:
                        self._buffer.append("\n")
                    return
                if lowered in {"ul", "ol"}:
                    self._flush()

            def handle_data(self, data: str):  # type: ignore[override]
                if self._skip_depth > 0:
                    return
                if self._current_type is None:
                    # Skip stray text outside any known block.
                    return
                self._buffer.append(data)

        walker = _Walker()
        walker.feed(html)
        walker.close()
        walker._flush()
        text = "\n".join(b.text for b in walker._blocks if b.text and b.text.strip())
        return walker._blocks, text


def _resolve_url(base: str | None, candidate: str | None) -> str | None:
    if not candidate:
        return None
    if base:
        return urljoin(base, candidate)
    return candidate
