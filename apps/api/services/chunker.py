"""Structure-prioritized parent/child chunker.

The chunker converts a parser-produced ``StructuredContent`` (a list of
typed ``Block`` entries with heading paths, page numbers and paragraph
indices) into one or more *parent* chunks plus smaller *child* chunks
suitable for retrieval.

Design rules (matching ``docs/ARCHITECTURE.md`` §3.4 and ADR-004/005):

* Sections are defined by top-level headings. Each section becomes a
  ``parent`` chunk that aggregates the heading and all of the content
  blocks up to the next sibling or higher heading.
* Children are produced from a section's body without breaking natural
  boundaries. Paragraphs, list items, code blocks, tables and block
  quotes are kept whole; long sequences are split at sentence
  boundaries (Chinese and English friendly). A hard length cap is
  applied as a last resort and the cap is configurable so the worker
  can re-tune it without code changes.
* Every chunk records its heading path, page, paragraph index and
  source span (``source_start`` / ``source_end``) so the UI can deep
  link back to the original document.
* The chunker is a pure function. Re-running it on the same
  ``structured_content`` always yields identical chunks, which makes
  the pipeline idempotent.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..parsers.base import Block, StructuredContent


CHILD_TARGET_MAX_CHARS = 600
CHILD_HARD_MAX_CHARS = 900
CHILD_TARGET_MIN_CHARS = 80
PARENT_SOFT_MAX_CHARS = 2_000
PARENT_HARD_MAX_CHARS = 3_500

# Sentence boundary detectors. Chinese punctuation first so we don't
# split on the ASCII period inside a number; newline is always a
# boundary. The English split also works on Latin script mixed in.
_CN_SENTENCE_RE = re.compile(r"(?<=[。！？!?；;])\s*")
_EN_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_PARAGRAPH_BREAK_RE = re.compile(r"\n\s*\n")

_CHUNK_ROLE_PARENT = "parent"
_CHUNK_ROLE_CHILD = "child"

_CHUNK_TYPE_BY_BLOCK = {
    "paragraph": "paragraph",
    "list_item": "list",
    "code_block": "code",
    "blockquote": "quote",
    "table": "table",
    "heading": "heading",
}


@dataclass
class ChunkSpec:
    """Pure data description of a chunk.

    The ``external_id`` is a deterministic identifier derived from the
    document version's content hash and the chunk's stable position
    within the document. It is used to relate parents and children
    *before* the database has assigned integer primary keys.
    """

    external_id: str
    parent_external_id: str | None
    order_index: int
    role: str
    chunk_type: str
    content: str
    content_hash: str
    heading_path: list[str]
    page: int | None
    paragraph_index: int | None
    source_start: int
    source_end: int
    char_count: int
    token_estimate: int
    language: str | None = None
    extra: dict = field(default_factory=dict)


def chunk_content_hash(content: str) -> str:
    """Stable hash of a chunk's content (UTF-8, SHA-256, hex digest)."""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_chunk_specs(
    structured: StructuredContent | dict,
    *,
    raw_text: str | None = None,
    content_hash_seed: str | None = None,
    child_max_chars: int = CHILD_TARGET_MAX_CHARS,
    child_hard_max_chars: int = CHILD_HARD_MAX_CHARS,
    child_min_chars: int = CHILD_TARGET_MIN_CHARS,
    parent_soft_max_chars: int = PARENT_SOFT_MAX_CHARS,
    parent_hard_max_chars: int = PARENT_HARD_MAX_CHARS,
) -> list[ChunkSpec]:
    """Return the parent and child chunks for a structured document.

    The function accepts either a :class:`StructuredContent` instance or
    a plain dictionary (as stored in ``document_versions.structured_content``).
    Both call styles are used: the worker usually works with the
    ORM model and the API may pass a freshly re-parsed dict from tests.
    """

    blocks, metadata, raw = _coerce(structured, raw_text)
    if not blocks:
        return []

    seed = content_hash_seed or chunk_content_hash(raw)
    sections = _split_into_sections(blocks)
    specs: list[ChunkSpec] = []
    parent_index = 0
    child_counter = 0
    for section_index, section in enumerate(sections):
        section_text = _join_section_text(section)
        if not section_text.strip():
            continue
        parent_external_id = f"{seed}:p:{section_index}"
        parent_hash = chunk_content_hash(section_text)
        parent = ChunkSpec(
            external_id=parent_external_id,
            parent_external_id=None,
            order_index=parent_index,
            role=_CHUNK_ROLE_PARENT,
            chunk_type="section",
            content=section_text,
            content_hash=parent_hash,
            heading_path=list(section.heading_path),
            page=section.page,
            paragraph_index=section.start_paragraph_index,
            source_start=section.source_start,
            source_end=section.source_end,
            char_count=len(section_text),
            token_estimate=_estimate_tokens(section_text),
            language=metadata.get("language") if isinstance(metadata, dict) else None,
        )
        specs.append(parent)
        parent_index += 1

        children = _split_section_into_children(
            section,
            content_seed=seed,
            parent_external_id=parent_external_id,
            target_max=child_max_chars,
            hard_max=child_hard_max_chars,
            min_chars=child_min_chars,
            parent_soft_max=parent_soft_max_chars,
            parent_hard_max=parent_hard_max_chars,
        )
        for child in children:
            child.parent_external_id = parent_external_id
            child.order_index = child_counter
            child.language = (
                metadata.get("language") if isinstance(metadata, dict) else None
            )
            specs.append(child)
            child_counter += 1
    return specs


@dataclass
class _Section:
    heading_path: list[str]
    page: int | None
    start_paragraph_index: int | None
    source_start: int
    source_end: int
    blocks: list[Block]
    heading_block: Block | None = None

    def body_text(self) -> str:
        return _join_section_text(self)


def _coerce(
    structured: StructuredContent | dict,
    raw_text: str | None,
) -> tuple[list[Block], dict, str]:
    if isinstance(structured, StructuredContent):
        blocks = list(structured.blocks)
        metadata = dict(structured.metadata or {})
        raw = raw_text if raw_text is not None else structured.full_text()
    elif isinstance(structured, dict):
        raw_blocks = structured.get("blocks") or []
        blocks = [_block_from_dict(b) for b in raw_blocks if isinstance(b, dict)]
        metadata = structured.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        raw = raw_text if raw_text is not None else _join_text(blocks)
    else:
        raise TypeError("structured must be a StructuredContent or dict")
    return blocks, metadata, raw


def _block_from_dict(payload: dict) -> Block:
    return Block(
        type=payload.get("type") or "paragraph",
        text=payload.get("text") or "",
        heading_path=list(payload.get("heading_path") or []),
        page=payload.get("page"),
        paragraph_index=payload.get("paragraph_index"),
        level=payload.get("level"),
    )


def _join_text(blocks: Iterable[Block]) -> str:
    return "\n".join(b.text for b in blocks if b.text and b.text.strip())


def _split_into_sections(blocks: Sequence[Block]) -> list[_Section]:
    """Group blocks into sections anchored by top-level headings.

    The very first heading seen in the document opens a section; any
    heading of the same or higher level closes the current section and
    starts a new one. We treat ``level == 1`` (and any heading at the
    start of the document) as a section boundary. Lower-level headings
    remain part of the current section but their text is still
    preserved in :attr:`heading_path`.
    """

    sections: list[_Section] = []
    current: _Section | None = None
    section_cursor = 0
    last_paragraph_index: int | None = None

    for block in blocks:
        text = (block.text or "").strip()
        if not text:
            continue
        is_heading = block.type == "heading"
        if is_heading:
            level = block.level or 1
            if current is None or level <= 1:
                if current is not None:
                    current.source_end = section_cursor
                    sections.append(current)
                heading_path = list(block.heading_path or [block.text])
                current = _Section(
                    heading_path=heading_path,
                    page=block.page,
                    start_paragraph_index=block.paragraph_index,
                    source_start=section_cursor,
                    source_end=section_cursor,
                    blocks=[],
                    heading_block=block,
                )
                section_cursor += len(text) + 2
            else:
                current.blocks.append(block)
                current.heading_path = list(block.heading_path or [block.text])
                section_cursor += len(text) + 2
        else:
            if current is None:
                current = _Section(
                    heading_path=[],
                    page=block.page,
                    start_paragraph_index=block.paragraph_index,
                    source_start=section_cursor,
                    source_end=section_cursor,
                    blocks=[],
                )
            current.blocks.append(block)
            last_paragraph_index = block.paragraph_index
            section_cursor += len(text) + 2

    if current is not None:
        current.source_end = section_cursor
        sections.append(current)
    return sections


def _join_section_text(section: _Section) -> str:
    pieces: list[str] = []
    if section.heading_block is not None and section.heading_block.text:
        pieces.append(section.heading_block.text.strip())
    for block in section.blocks:
        if block.type == "heading" and block is section.heading_block:
            continue
        if block.text and block.text.strip():
            pieces.append(block.text.strip())
    return "\n\n".join(pieces).strip()


def _split_section_into_children(
    section: _Section,
    *,
    content_seed: str,
    parent_external_id: str,
    target_max: int,
    hard_max: int,
    min_chars: int,
    parent_soft_max: int,
    parent_hard_max: int,
) -> list[ChunkSpec]:
    """Split a section's body into child chunks.

    Children are produced greedily by accumulating the section's blocks
    (or sentence fragments) until the soft target is reached. If a
    single block exceeds ``hard_max`` it is split at sentence
    boundaries and, as a last resort, by character.
    """

    if not section.blocks:
        return []

    body_blocks = [
        b for b in section.blocks if b.text and b.text.strip()
    ]
    full_text = _join_section_text(section)
    body_pages = {b.page for b in body_blocks if b.page is not None}
    if len(full_text) <= target_max and len(body_pages) <= 1:
        first_paragraph_index = (
            body_blocks[0].paragraph_index
            if body_blocks and body_blocks[0].paragraph_index is not None
            else section.start_paragraph_index
        )
        first_page = (
            body_blocks[0].page
            if body_blocks and body_blocks[0].page is not None
            else section.page
        )
        return [
            _make_child_chunk(
                external_id=f"{parent_external_id}:c:0",
                content=full_text,
                chunk_type=_classify_blocks(body_blocks),
                heading_path=section.heading_path,
                page=first_page,
                paragraph_index=first_paragraph_index,
                source_start=section.source_start,
                source_end=section.source_end,
            )
        ]

    # Pre-compute each block's char offset relative to the section so
    # we can record accurate source spans without re-scanning the text.
    heading_offset = (
        len(section.heading_block.text.strip()) + 2
        if section.heading_block is not None and section.heading_block.text
        else 0
    )
    block_offsets: list[tuple[Block, int, int]] = []
    cursor = heading_offset
    for block in body_blocks:
        text = block.text.strip()
        block_offsets.append((block, cursor, cursor + len(text)))
        cursor += len(text) + 2  # +2 for the "\n\n" join

    children: list[ChunkSpec] = []
    buffer: list[tuple[Block, int, int]] = []
    buffer_text_len = 0
    child_index = 0

    def flush_buffer() -> int:
        nonlocal buffer_text_len, child_index
        if not buffer:
            return 0
        joined = "\n\n".join(b.text.strip() for b, _, _ in buffer if b.text)
        if not joined.strip():
            buffer.clear()
            buffer_text_len = 0
            return 0
        start_offset = buffer[0][1]
        end_offset = buffer[-1][2]
        spec = _make_child_chunk(
            external_id=f"{parent_external_id}:c:{child_index}",
            content=joined,
            chunk_type=_classify_blocks([b for b, _, _ in buffer]),
            heading_path=section.heading_path,
            page=(
                buffer[0][0].page
                if buffer[0][0].page is not None
                else section.page
            ),
            paragraph_index=(
                buffer[0][0].paragraph_index
                if buffer[0][0].paragraph_index is not None
                else section.start_paragraph_index
            ),
            source_start=section.source_start + start_offset,
            source_end=section.source_start + end_offset,
        )
        children.append(spec)
        child_index += 1
        flushed = len(buffer)
        buffer.clear()
        buffer_text_len = 0
        return flushed

    for block, start, end in block_offsets:
        text = block.text.strip()
        text_len = len(text)
        if text_len > hard_max:
            flush_buffer()
            sub_pieces = _split_long_text(
                text,
                target=target_max,
                hard_max=hard_max,
                min_chars=min_chars,
            )
            cursor_local = start
            for sub_text in sub_pieces:
                end_local = cursor_local + len(sub_text)
                spec = _make_child_chunk(
                    external_id=f"{parent_external_id}:c:{child_index}",
                    content=sub_text,
                    chunk_type=block.type,
                    heading_path=section.heading_path,
                    page=block.page if block.page is not None else section.page,
                    paragraph_index=block.paragraph_index,
                    source_start=section.source_start + cursor_local,
                    source_end=section.source_start + end_local,
                )
                children.append(spec)
                child_index += 1
                cursor_local = end_local + 2
            continue

        page_changed = bool(
            buffer
            and block.page is not None
            and buffer[0][0].page is not None
            and block.page != buffer[0][0].page
        )
        if buffer and (page_changed or buffer_text_len + text_len > target_max):
            flush_buffer()
        buffer.append((block, start, end))
        buffer_text_len += text_len + 2

    flush_buffer()
    return children


def _split_long_text(
    text: str,
    *,
    target: int,
    hard_max: int,
    min_chars: int,
) -> list[str]:
    if len(text) <= hard_max:
        return [text]
    pieces: list[str] = []
    # First try splitting by paragraph breaks.
    paragraphs = [p.strip() for p in _PARAGRAPH_BREAK_RE.split(text) if p.strip()]
    if len(paragraphs) > 1:
        for paragraph in paragraphs:
            if len(paragraph) > hard_max:
                pieces.extend(
                    _split_long_text(
                        paragraph,
                        target=target,
                        hard_max=hard_max,
                        min_chars=min_chars,
                    )
                )
            else:
                pieces.append(paragraph)
        if pieces:
            return _merge_short_pieces(pieces, target=target, min_chars=min_chars)
    sentences = _split_sentences(text)
    if len(sentences) <= 1:
        return _hard_split(text, hard_max)
    return _merge_short_pieces(sentences, target=target, min_chars=min_chars)


def _split_sentences(text: str) -> list[str]:
    # Apply Chinese punctuation first, then English on any leftover.
    pieces: list[str] = []
    for chunk in _CN_SENTENCE_RE.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        sub = [s.strip() for s in _EN_SENTENCE_RE.split(chunk) if s.strip()]
        if len(sub) > 1:
            pieces.extend(sub)
        else:
            pieces.append(chunk)
    return pieces or [text.strip()]


def _merge_short_pieces(
    pieces: list[str], *, target: int, min_chars: int
) -> list[str]:
    merged: list[str] = []
    buffer = ""
    for piece in pieces:
        candidate = (buffer + " " + piece).strip() if buffer else piece
        if buffer and len(candidate) > target:
            merged.append(buffer)
            buffer = piece
        else:
            buffer = candidate
    if buffer:
        merged.append(buffer)
    if min_chars <= 0:
        return merged
    # If the last fragment is too small, fold it into the previous one
    # so the index doesn't end up with dangling noise.
    if len(merged) >= 2 and len(merged[-1]) < min_chars:
        merged[-2] = (merged[-2] + " " + merged[-1]).strip()
        merged.pop()
    return merged


def _hard_split(text: str, hard_max: int) -> list[str]:
    if hard_max <= 0:
        return [text]
    return [text[i : i + hard_max] for i in range(0, len(text), hard_max)]


def _classify_blocks(blocks: Sequence[Block]) -> str:
    if not blocks:
        return "section"
    types = {b.type for b in blocks}
    if len(types) == 1:
        only = next(iter(types))
        return _CHUNK_TYPE_BY_BLOCK.get(only, only)
    return "mixed"


def _make_child_chunk(
    *,
    external_id: str,
    content: str,
    chunk_type: str,
    heading_path: Sequence[str],
    page: int | None,
    paragraph_index: int | None,
    source_start: int,
    source_end: int,
) -> ChunkSpec:
    return ChunkSpec(
        external_id=external_id,
        parent_external_id=None,  # filled by the caller
        order_index=0,
        role=_CHUNK_ROLE_CHILD,
        chunk_type=chunk_type or "section",
        content=content,
        content_hash=chunk_content_hash(content),
        heading_path=list(heading_path),
        page=page,
        paragraph_index=paragraph_index,
        source_start=source_start,
        source_end=source_end,
        char_count=len(content),
        token_estimate=_estimate_tokens(content),
    )


def _estimate_tokens(text: str) -> int:
    """Rough token estimate that works for mixed CJK/Latin text.

    We approximate Latin words at ~4 characters per token and CJK
    characters at 1 token each. The estimate is intentionally rough
    because the API only surfaces it for observability, not billing.
    """

    if not text:
        return 0
    cjk = sum(1 for c in text if _is_cjk(c))
    remaining = len(text) - cjk
    return cjk + max(1, remaining // 4)


def _is_cjk(char: str) -> bool:
    if not char:
        return False
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF  # Hiragana / Katakana
        or 0x3400 <= code <= 0x4DBF  # CJK Extension A
        or 0x4E00 <= code <= 0x9FFF  # CJK Unified Ideographs
        or 0xF900 <= code <= 0xFAFF  # CJK Compatibility Ideographs
    )
