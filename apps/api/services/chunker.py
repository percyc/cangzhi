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
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ..parsers.base import Block, StructuredContent

CHILD_TARGET_MAX_CHARS = 600
CHILD_HARD_MAX_CHARS = 900
CHILD_TARGET_MIN_CHARS = 80
CHILD_OVERLAP_CHARS = 120
PARENT_MAX_CHARS = 80_000
DATASET_CATALOG_SAMPLE_ROWS = 12
DATASET_CATALOG_MAX_ROW_CHARS = 1_000

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
    child_overlap_chars: int = CHILD_OVERLAP_CHARS,
    preserve_source_spans: bool = False,
) -> list[ChunkSpec]:
    """Return the parent and child chunks for a structured document.

    The function accepts either a :class:`StructuredContent` instance or
    a plain dictionary (as stored in ``document_versions.structured_content``).
    Both call styles are used: the worker usually works with the
    ORM model and the API may pass a freshly re-parsed dict from tests.
    """

    document_type = (
        structured.document_type
        if isinstance(structured, StructuredContent)
        else str(structured.get("document_type") or "")
    )
    blocks, metadata, raw = _coerce(
        structured,
        raw_text,
        include_raw=content_hash_seed is None,
    )
    if not blocks:
        return []

    seed = content_hash_seed or chunk_content_hash(raw)
    if document_type in {"xlsx", "xls"}:
        # Excel participates in retrieval through validated datasets only.
        # Irregular regions remain in the versioned parser payload and original
        # workbook for diagnosis/governance, but are not flattened into prose
        # chunks.  Treating an arbitrary workbook as a long document creates
        # expensive vectors while silently discarding its two-dimensional
        # semantics.
        dataset_blocks = [
            block
            for block in blocks
            if (block.extra or {}).get("dataset_eligible") is not False
        ]
        return _build_dataset_catalog_specs(
            dataset_blocks,
            metadata=metadata,
            seed=seed,
        )
    sections = _split_into_sections(blocks)
    specs: list[ChunkSpec] = []
    parent_index = 0
    child_counter = 0
    for section_index, section in enumerate(sections):
        section_text = _join_section_text(section)
        if not section_text.strip():
            continue
        parent_external_id = f"{seed}:p:{section_index}"
        parent_content, parent_truncated = _bounded_parent_content(section_text)
        parent_extra = _merge_block_extras(section.blocks)
        if parent_truncated:
            parent_extra = {
                **parent_extra,
                "parent_content_truncated": True,
                "original_char_count": len(section_text),
            }
        parent_hash = chunk_content_hash(parent_content)
        parent = ChunkSpec(
            external_id=parent_external_id,
            parent_external_id=None,
            order_index=parent_index,
            role=_CHUNK_ROLE_PARENT,
            chunk_type="section",
            content=parent_content,
            content_hash=parent_hash,
            heading_path=list(section.heading_path),
            page=section.page,
            paragraph_index=section.start_paragraph_index,
            source_start=section.source_start,
            source_end=section.source_end,
            char_count=len(parent_content),
            token_estimate=_estimate_tokens(parent_content),
            language=metadata.get("language") if isinstance(metadata, dict) else None,
            extra=parent_extra,
        )
        specs.append(parent)
        parent_index += 1

        children = _split_section_into_children(
            section,
            content_seed=seed,
            parent_external_id=parent_external_id,
            target_max=max(
                child_min_chars,
                child_max_chars - child_overlap_chars,
            ),
            hard_max=max(
                child_min_chars,
                child_hard_max_chars - child_overlap_chars,
            ),
            min_chars=child_min_chars,
            preserve_source_spans=preserve_source_spans,
        )
        _apply_child_overlap(
            children,
            overlap_chars=child_overlap_chars,
            hard_max=child_hard_max_chars,
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


def _build_dataset_catalog_specs(
    blocks: Sequence[Block],
    *,
    metadata: dict,
    seed: str,
) -> list[ChunkSpec]:
    """Build compact discovery chunks for spreadsheet data regions.

    Spreadsheet rows remain fully queryable in ``structured_table_rows``.
    Repeating them in the document chunk/vector layer is both expensive and
    misleading: vector retrieval discovers a dataset, while the structured
    executor performs complete filtering and aggregation.
    """

    grouped: dict[tuple[str, int], list[Block]] = {}
    for block in blocks:
        if block.type != "table":
            continue
        extra = block.extra or {}
        key = (
            str(extra.get("sheet_name") or (block.heading_path or ["工作表"])[0]),
            int(extra.get("region_index") or 1),
        )
        grouped.setdefault(key, []).append(block)

    specs: list[ChunkSpec] = []
    for dataset_order, ((sheet_name, region_index), table_blocks) in enumerate(
        grouped.items()
    ):
        extras = [block.extra or {} for block in table_blocks]
        columns = list(
            dict.fromkeys(
                str(column)
                for extra in extras
                for column in (extra.get("column_names") or [])
                if column
            )
        )
        row_start = min(
            (int(extra["row_start"]) for extra in extras if extra.get("row_start") is not None),
            default=None,
        )
        row_end = max(
            (int(extra["row_end"]) for extra in extras if extra.get("row_end") is not None),
            default=None,
        )
        header_row = next(
            (extra.get("header_row") for extra in extras if extra.get("header_row") is not None),
            None,
        )
        row_count = sum(
            len([line for line in (block.text or "").splitlines() if line.strip()])
            for block in table_blocks
        )
        samples = _representative_table_rows(table_blocks)
        display_name = (
            sheet_name
            if region_index == 1
            else f"{sheet_name} · 数据区域 {region_index}"
        )
        content = "\n".join(
            [
                f"数据集：{display_name}",
                f"工作表：{sheet_name}",
                f"规模：{row_count} 行，{len(columns)} 个字段",
                f"字段：{'、'.join(columns)[:4_000] or '未识别'}",
                (
                    "用途：这是可计算的结构化数据。精确筛选、计数、求和、分组和排序"
                    "应由数据集查询执行器处理，不以代表行推断全量事实。"
                ),
                *(["代表行：", *samples] if samples else []),
            ]
        )
        dataset_key = chunk_content_hash(f"{sheet_name}\0{region_index}")[:16]
        parent_external_id = f"{seed}:dataset:{dataset_key}:p"
        common_extra = {
            "dataset_catalog": True,
            "sheet_name": sheet_name,
            "region_index": region_index,
            "row_start": row_start,
            "row_end": row_end,
            "header_row": header_row,
            "column_names": columns,
            "catalog_row_count": row_count,
            "catalog_sample_count": len(samples),
        }
        heading_path = [sheet_name, f"数据区域 {region_index}"]
        paragraph_index = table_blocks[0].paragraph_index
        source_start = row_start or 0
        source_end = row_end or source_start
        specs.extend(
            [
                ChunkSpec(
                    external_id=parent_external_id,
                    parent_external_id=None,
                    order_index=dataset_order,
                    role=_CHUNK_ROLE_PARENT,
                    chunk_type="dataset",
                    content=content,
                    content_hash=chunk_content_hash(content),
                    heading_path=heading_path,
                    page=None,
                    paragraph_index=paragraph_index,
                    source_start=source_start,
                    source_end=source_end,
                    char_count=len(content),
                    token_estimate=_estimate_tokens(content),
                    language=metadata.get("language"),
                    extra=common_extra,
                ),
                ChunkSpec(
                    external_id=f"{seed}:dataset:{dataset_key}:c",
                    parent_external_id=parent_external_id,
                    order_index=dataset_order,
                    role=_CHUNK_ROLE_CHILD,
                    chunk_type="dataset_catalog",
                    content=content,
                    content_hash=chunk_content_hash(content),
                    heading_path=heading_path,
                    page=None,
                    paragraph_index=paragraph_index,
                    source_start=source_start,
                    source_end=source_end,
                    char_count=len(content),
                    token_estimate=_estimate_tokens(content),
                    language=metadata.get("language"),
                    extra=common_extra,
                ),
            ]
        )
    return specs


def _representative_table_rows(blocks: Sequence[Block]) -> list[str]:
    if not blocks:
        return []
    if len(blocks) <= DATASET_CATALOG_SAMPLE_ROWS:
        block_indexes = list(range(len(blocks)))
    else:
        block_indexes = sorted(
            {
                round(index * (len(blocks) - 1) / (DATASET_CATALOG_SAMPLE_ROWS - 1))
                for index in range(DATASET_CATALOG_SAMPLE_ROWS)
            }
        )
    samples: list[str] = []
    for block_index in block_indexes:
        lines = [line.strip() for line in (blocks[block_index].text or "").splitlines() if line.strip()]
        if not lines:
            continue
        for line in (lines[0], lines[-1]):
            sample = line[:DATASET_CATALOG_MAX_ROW_CHARS]
            if sample not in samples:
                samples.append(sample)
            if len(samples) >= DATASET_CATALOG_SAMPLE_ROWS:
                return samples
    return samples


def _bounded_parent_content(content: str) -> tuple[str, bool]:
    """Bound non-retrieval parent text while retaining both source edges."""

    if len(content) <= PARENT_MAX_CHARS:
        return content, False
    marker = "\n\n…父级上下文过长，中间内容已省略；完整内容请读取原文或子切片…\n\n"
    available = PARENT_MAX_CHARS - len(marker)
    head_chars = available // 2
    tail_chars = available - head_chars
    return f"{content[:head_chars]}{marker}{content[-tail_chars:]}", True


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
    *,
    include_raw: bool = True,
) -> tuple[list[Block], dict, str]:
    if isinstance(structured, StructuredContent):
        blocks = list(structured.blocks)
        metadata = dict(structured.metadata or {})
        raw = (
            raw_text
            if raw_text is not None
            else structured.full_text()
            if include_raw
            else ""
        )
    elif isinstance(structured, dict):
        raw_blocks = structured.get("blocks") or []
        blocks = [_block_from_dict(b) for b in raw_blocks if isinstance(b, dict)]
        metadata = structured.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        raw = (
            raw_text
            if raw_text is not None
            else _join_text(blocks)
            if include_raw
            else ""
        )
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
        extra=dict(payload.get("extra") or {}),
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
    preserve_source_spans: bool = False,
) -> list[ChunkSpec]:
    """Split a section's body into child chunks.

    Children are produced greedily by accumulating the section's blocks
    (or sentence fragments) until the soft target is reached. If a
    single block exceeds ``hard_max`` it is split at sentence
    boundaries and, as a last resort, by character.

    Parent chunks intentionally retain the complete semantic section.
    Only child chunks are size-bounded because they are the retrieval units.
    """

    if not section.blocks:
        return []

    body_blocks = [
        b for b in section.blocks if b.text and b.text.strip()
    ]
    full_text = _join_section_text(section)
    body_pages = {b.page for b in body_blocks if b.page is not None}

    # If parent is within limits, return as single child chunk normally
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
                extra=_merge_block_extras(body_blocks),
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
            extra=_merge_block_extras([b for b, _, _ in buffer]),
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
            if preserve_source_spans:
                row_matches = list(_TABLE_ROW_RE.finditer(text)) if block.type == "table" else []
                row_starts = [match.start() for match in row_matches]
                for piece_start, piece_end in _split_source_spans(
                    text, target=target_max, hard_max=hard_max,
                    table=block.type in {"table", "code_block"}
                ):
                    sub_text = text[piece_start:piece_end]
                    extra = dict(block.extra)
                    if block.type == "table":
                        # Serialized row labels remain source text, never
                        # manufacture a repeated label in continuation pieces.
                        first = max(0, bisect_right(row_starts, piece_start) - 1)
                        last = bisect_right(row_starts, piece_end - 1)
                        rows = [int(match.group(1)) for match in row_matches[first:last]]
                        if rows:
                            extra.update(row_start=min(rows), row_end=max(rows))
                        if (piece_start and text[piece_start - 1] != "\n") or (
                            piece_end < len(text) and text[piece_end - 1] != "\n"
                            and text[piece_end] not in "\r\n"
                        ):
                            extra["row_fragmented"] = True
                    extra["source_span_kind"] = "exact_core"
                    children.append(_make_child_chunk(
                        external_id=f"{parent_external_id}:c:{child_index}",
                        content=sub_text, chunk_type=block.type,
                        heading_path=section.heading_path,
                        page=block.page if block.page is not None else section.page,
                        paragraph_index=block.paragraph_index,
                        source_start=section.source_start + start + piece_start,
                        source_end=section.source_start + start + piece_end,
                        extra=extra,
                    ))
                    child_index += 1
                continue
            sub_pieces = (
                _split_table_text(
                    text,
                    target=target_max,
                    hard_max=hard_max,
                )
                if block.type == "table"
                else _split_long_text(
                    text,
                    target=target_max,
                    hard_max=hard_max,
                    min_chars=min_chars,
                )
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
                    extra=(
                        _table_piece_extra(block.extra, sub_text)
                        if block.type == "table"
                        else dict(block.extra)
                    ),
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


def _split_source_spans(text: str, *, target: int, hard_max: int, table: bool = False):
    """Bounded, monotonic original-text ranges; never rejoin normalized sentences.

    Prefer row ends for tables and paragraph/sentence ends for prose. Split an
    oversized atomic unit only as a last resort. Preserve whitespace (including
    code indentation); only an entirely whitespace-only piece is not indexed.
    Enabled by structural candidates, not existing ingestion strategies.
    """
    if hard_max <= 0 or target <= 0:
        raise ValueError("positive split sizes required")
    pattern = r"\r?\n" if table else r"\n\s*\n|[。！？!?；;]\s*|[.!?]\s+"
    boundaries = [match.end() for match in re.finditer(pattern, text)]
    cursor = 0
    while cursor < len(text):
        limit = min(len(text), cursor + hard_max)
        if limit == len(text):
            end = limit
        else:
            goal = min(limit, cursor + target)
            before = bisect_right(boundaries, goal) - 1
            after = bisect_right(boundaries, goal)
            if before >= 0 and boundaries[before] > cursor:
                end = boundaries[before]
            elif after < len(boundaries) and boundaries[after] <= limit:
                end = boundaries[after]
            else:
                end = limit
        if text[cursor:end].strip():
            yield cursor, end
        cursor = end


def _apply_child_overlap(
    children: list[ChunkSpec],
    *,
    overlap_chars: int,
    hard_max: int,
) -> None:
    """Prepend a bounded semantic tail from the previous child.

    ``source_start``/``source_end`` continue to describe the expanded
    retrieval text, while ``extra.core_source_*`` records the unique
    non-overlapping span.  This makes boundary context available to
    lexical/vector retrieval without losing exact coverage metadata.
    """

    if overlap_chars <= 0 or len(children) < 2:
        return
    for child in children:
        child.extra.setdefault("core_source_start", child.source_start)
        child.extra.setdefault("core_source_end", child.source_end)
        child.extra.setdefault("overlap_prefix_chars", 0)
    previous_core = children[0].content
    previous_core_start = children[0].source_start
    previous_core_end = children[0].source_end
    for child in children[1:]:
        core_content = child.content
        if child.chunk_type == "table" and (
            child.extra.get("sheet_name") or child.extra.get("table_ranges")
        ):
            previous_core = core_content
            previous_core_start = child.source_start
            previous_core_end = child.source_end
            continue
        available = max(0, hard_max - len(core_content) - 2)
        prefix = _semantic_tail(
            previous_core,
            max_chars=min(overlap_chars, available),
        )
        if not prefix:
            previous_core = core_content
            continue
        core_start = child.source_start
        core_end = child.source_end
        child.content = f"{prefix}\n\n{core_content}"
        child.source_start = max(previous_core_start, previous_core_end - len(prefix))
        child.content_hash = chunk_content_hash(child.content)
        child.char_count = len(child.content)
        child.token_estimate = _estimate_tokens(child.content)
        child.extra.update(
            {
                "core_source_start": core_start,
                "core_source_end": core_end,
                "overlap_prefix_chars": len(prefix),
            }
        )
        previous_core = core_content
        previous_core_start = core_start
        previous_core_end = core_end


def _semantic_tail(text: str, *, max_chars: int) -> str:
    if max_chars <= 0 or not text:
        return ""
    sentences = _split_sentences(text)
    selected: list[str] = []
    length = 0
    for sentence in reversed(sentences):
        addition = len(sentence) + (1 if selected else 0)
        if selected and length + addition > max_chars:
            break
        if not selected and len(sentence) > max_chars:
            return sentence[-max_chars:].strip()
        selected.append(sentence)
        length += addition
    return " ".join(reversed(selected)).strip()


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


def _split_table_text(text: str, *, target: int, hard_max: int) -> list[str]:
    """Split tabular text on row boundaries before falling back to hard cuts."""
    rows = [row for row in text.splitlines() if row.strip()]
    if len(rows) <= 1:
        return _hard_split(text, hard_max)
    pieces: list[str] = []
    buffer: list[str] = []
    buffer_length = 0
    for row in rows:
        if len(row) > hard_max:
            if buffer:
                pieces.append("\n".join(buffer))
                buffer = []
                buffer_length = 0
            locator_match = re.match(r"^(行\s+\d+｜)", row)
            if locator_match is None:
                pieces.extend(_hard_split(row, hard_max))
                continue
            locator = locator_match.group(1)
            body = row[len(locator) :]
            fragment_size = max(1, hard_max - len(locator) - 12)
            fragments = _hard_split(body, fragment_size)
            pieces.extend(
                f"{locator}【片段 {index}/{len(fragments)}】{fragment}"
                for index, fragment in enumerate(fragments, start=1)
            )
            continue
        candidate_length = buffer_length + (1 if buffer else 0) + len(row)
        if buffer and candidate_length > target:
            pieces.append("\n".join(buffer))
            buffer = [row]
            buffer_length = len(row)
        else:
            buffer.append(row)
            buffer_length = candidate_length
    if buffer:
        pieces.append("\n".join(buffer))
    return pieces


_TABLE_ROW_RE = re.compile(r"^行\s+(\d+)｜", re.MULTILINE)


def _table_piece_extra(base: dict, text: str) -> dict:
    extra = dict(base or {})
    rows = [int(value) for value in _TABLE_ROW_RE.findall(text)]
    if rows:
        extra["row_start"] = min(rows)
        extra["row_end"] = max(rows)
    if "【片段 " in text:
        extra["row_fragmented"] = True
    return extra


def _merge_block_extras(blocks: Sequence[Block]) -> dict:
    block_extras = [dict(block.extra) for block in blocks if block.extra]
    if not block_extras:
        return {}

    ocr_extras = [item for item in block_extras if item.get("source") == "ocr"]
    if ocr_extras:
        merged_ocr = dict(ocr_extras[0])
        bboxes = [
            item["bbox"]
            for item in ocr_extras
            if isinstance(item.get("bbox"), list) and len(item["bbox"]) == 4
        ]
        if bboxes:
            merged_ocr["bbox"] = [
                min(float(bbox[0]) for bbox in bboxes),
                min(float(bbox[1]) for bbox in bboxes),
                max(float(bbox[2]) for bbox in bboxes),
                max(float(bbox[3]) for bbox in bboxes),
            ]
        confidences = [
            float(item["confidence"])
            for item in ocr_extras
            if isinstance(item.get("confidence"), (int, float))
        ]
        if confidences:
            merged_ocr["confidence"] = round(
                sum(confidences) / len(confidences),
                4,
            )
        return merged_ocr

    table_ranges = block_extras
    if len(table_ranges) == 1:
        return table_ranges[0]
    regions = {
        (item.get("sheet_name"), item.get("region_index"))
        for item in table_ranges
    }
    if len(regions) == 1:
        merged = dict(table_ranges[0])
        starts = [
            item["row_start"]
            for item in table_ranges
            if item.get("row_start") is not None
        ]
        ends = [
            item["row_end"]
            for item in table_ranges
            if item.get("row_end") is not None
        ]
        if starts:
            merged["row_start"] = min(starts)
        if ends:
            merged["row_end"] = max(ends)
        return merged
    return {"table_ranges": table_ranges}


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
    extra: dict | None = None,
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
        extra=dict(extra or {}),
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
