"""Exact candidate coordinates, independently of aggregate parent coverage."""
import re

import pytest

from apps.api.parsers.base import Block, StructuredContent
from apps.api.services.chunker import build_chunk_specs


@pytest.mark.parametrize("kind,body", [
    ("paragraph", "连续中文句子。下一句没有空格；最后是条件！" * 70),
    ("paragraph", "Repeated sentence.  Another sentence!\tA condition? " * 70),
    ("paragraph", "甲" * 1800),
    ("paragraph", "同一句。" * 400),
    ("paragraph", "First paragraph.\r\n\r\n\tSecond\tline.  \n\n" * 60),
    ("table", "\r\n".join(f"row{i}\tA\tB" for i in range(180))),
    ("table", "column\tvalue\n" + "X" * 1800 + "\nend\tvalue"),
    ("code_block", "if ok:\n    print('data')\n\n" * 100),
])
@pytest.mark.parametrize("overlap", [0, 20])
def test_long_block_children_are_original_slices(kind, body, overlap):
    heading = "Section"
    source = heading + "\n\n" + body.strip()
    structured = StructuredContent(document_type="docx", blocks=[
        Block("heading", heading, [heading], level=1, page=3, paragraph_index=11),
        Block(kind, body, [heading], page=3, paragraph_index=12),
    ])
    specs = build_chunk_specs(structured, preserve_source_spans=True,
        child_max_chars=100, child_hard_max_chars=160, child_min_chars=20,
        child_overlap_chars=overlap)
    children = [spec for spec in specs if spec.role == "child"]
    assert len(children) > 2
    cores = []
    previous_end = len(heading) + 2
    for child in children:
        prefix = child.extra.get("overlap_prefix_chars", 0)
        content = child.content[prefix + 2:] if prefix else child.content
        start = child.extra.get("core_source_start", child.source_start)
        end = child.extra.get("core_source_end", child.source_end)
        assert 0 <= start < end <= len(source)
        assert start >= previous_end
        assert not source[previous_end:start].strip()
        assert content == source[start:end]
        assert child.page == 3 and child.paragraph_index == 12
        assert child.char_count <= 160
        cores.append(content)
        previous_end = end
    assert not source[previous_end:].strip()
    assert re.sub(r"\s", "", "".join(cores)) == re.sub(r"\s", "", body)
    assert "".join(cores) == body.strip()


def test_oversized_serialized_row_has_real_text_and_row_metadata():
    body = "行 2｜" + "很长的字段" * 200 + "\n行 3｜最后一行"
    structured = StructuredContent(document_type="docx", blocks=[
        Block("table", body, [], extra={"sheet_name": "details", "row_start": 2, "row_end": 3})
    ])
    children = [spec for spec in build_chunk_specs(structured, preserve_source_spans=True,
        child_max_chars=100, child_hard_max_chars=160, child_min_chars=20,
        child_overlap_chars=0) if spec.role == "child"]
    assert len(children) > 2
    assert any(child.extra.get("row_fragmented") for child in children)
    for child in children:
        assert child.content == body[child.source_start:child.source_end]
        assert "【片段" not in child.content
        assert child.extra["row_start"] in (2, 3)
        assert child.extra["row_end"] in (2, 3)
    assert children[1].extra["row_start"] == 2
    assert children[-1].extra["row_end"] == 3


def test_existing_strategy_is_not_implicitly_changed():
    structured = StructuredContent(document_type="txt", blocks=[
        Block("paragraph", "Long sentence.\n\nSecond paragraph. " * 100, [])
    ])
    assert build_chunk_specs(structured) == build_chunk_specs(structured, preserve_source_spans=False)
