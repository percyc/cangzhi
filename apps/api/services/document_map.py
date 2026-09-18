"""Pure, version-pinned source navigation. No model calls or serving-index writes."""
from __future__ import annotations

import hashlib
import json


def build_document_map(payload: dict, *, version_id: int) -> dict:
    if type(version_id) is not int or version_id <= 0:
        raise ValueError("invalid_version_id")
    if not isinstance(payload, dict):
        raise ValueError("invalid_payload")
    blocks = payload.get("blocks", [])
    if not isinstance(blocks, list):
        raise ValueError("invalid_blocks")
    # Processing metadata is not evidence identity. Changes to source structure
    # deliberately invalidate this snapshot; historical reads need its saved copy.
    try:
        fingerprint = hashlib.sha256(json.dumps(
            {"document_type": payload.get("document_type", "txt"), "blocks": blocks},
            sort_keys=True, ensure_ascii=False, allow_nan=False,
        ).encode()).hexdigest()
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid_source_structure") from exc
    entries = []
    # Keep empty blocks and repeated text: positional identity is intentional.
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            raise ValueError("invalid_block")
        if block.get("type", "paragraph") not in {
            "heading", "paragraph", "list_item", "table", "code_block", "blockquote",
        }:
            raise ValueError("invalid_block_type")
        for field in ("page", "paragraph_index"):
            value = block.get(field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("invalid_block_location")
        path = block.get("heading_path", [])
        if not isinstance(path, list) or any(not isinstance(s, str) for s in path):
            raise ValueError("invalid_heading_path")
        text = block["text"]
        entries.append({
            "id": f"v{version_id}:{fingerprint}:b{index}",
            "block_index": index,
            "type": block.get("type", "paragraph"),
            "heading_path": list(path),
            "page": block.get("page"),
            "paragraph_index": block.get("paragraph_index"),
            "char_count": len(text),
        })
    return {"schema_version": 1, "version_id": version_id,
            "source_fingerprint": fingerprint,
            "document_type": payload.get("document_type", "txt"),
            "blocks": entries, "total_blocks": len(entries)}


def read_map_block(payload: dict, *, version_id: int, block_id: str,
                   offset: int = 0, max_chars: int = 4000) -> dict:
    """Bounded exact source text; callers must enforce document authorization.

    Block-local offsets are not offsets into raw_content or rendered PDF text.
    An oversized table can be paged, but is not presented as complete rows.
    """
    if type(offset) is not int or offset < 0:
        raise ValueError("invalid_offset")
    if type(max_chars) is not int or not 1 <= max_chars <= 12000:
        raise ValueError("invalid_max_chars")
    document_map = build_document_map(payload, version_id=version_id)
    entry = next((b for b in document_map["blocks"] if b["id"] == block_id), None)
    if entry is None:
        raise ValueError("block_not_found_or_source_changed")
    text = payload["blocks"][entry["block_index"]]["text"]
    if offset > len(text):
        raise ValueError("offset_out_of_range")
    end = min(offset + max_chars, len(text))
    return {**entry, "text": text[offset:end], "offset": offset,
            "next_offset": end if end < len(text) else None,
            "partial": offset > 0 or end < len(text),
            "source_fingerprint": document_map["source_fingerprint"]}


def plan_source_windows(payload: dict, *, version_id: int,
                        max_chars: int = 6000, max_segments: int = 32) -> list[dict]:
    """Cover all nonempty source characters without silently truncating long blocks.

    Limits bound source text, not serialized prompt size or actual model tokens.
    Split tables are explicitly partial; these windows must not become chunks.
    """
    if type(max_chars) is not int or not 1 <= max_chars <= 12000:
        raise ValueError("invalid_max_chars")
    if type(max_segments) is not int or not 1 <= max_segments <= 64:
        raise ValueError("invalid_max_segments")
    document_map = build_document_map(payload, version_id=version_id)
    windows, segments, used = [], [], 0
    for entry in document_map["blocks"]:
        text = payload["blocks"][entry["block_index"]]["text"]
        # A new explicit heading starts a window; parser titles remain evidence,
        # not model-inferred section identities.
        if entry["type"] == "heading" and segments:
            windows.append({"segments": segments, "source_chars": used})
            segments, used = [], 0
        start = 0
        while start < len(text):
            if used == max_chars or len(segments) == max_segments:
                windows.append({"segments": segments, "source_chars": used})
                segments, used = [], 0
            stop = min(start + max_chars - used, len(text))
            segments.append({"block_id": entry["id"], "block_index": entry["block_index"],
                             "start": start, "stop": stop,
                             "partial_block": start > 0 or stop < len(text)})
            used += stop - start
            start = stop
    if segments:
        windows.append({"segments": segments, "source_chars": used})
    return windows
