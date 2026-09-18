import copy

import pytest

from apps.api.services.document_map import build_document_map, read_map_block, plan_source_windows


def source(kind="docx"):
    return {"document_type": kind, "blocks": [
        {"type": "heading", "text": "第一章", "heading_path": [], "page": 1},
        {"type": "paragraph", "text": "", "heading_path": ["第一章"]},
        {"type": "table", "text": "甲|乙\n1|2\n3|4", "heading_path": ["第一章"]},
        {"type": "paragraph", "text": "第一章", "heading_path": ["第一章"]},
    ]}


@pytest.mark.parametrize("kind", ["pdf", "doc", "docx", "markdown", "html", "note", "xlsx"])
def test_map_preserves_order_empty_blocks_and_source(kind):
    payload = source(kind)
    before = copy.deepcopy(payload)
    result = build_document_map(payload, version_id=4)
    assert payload == before
    assert result == build_document_map(payload, version_id=4)
    assert result["total_blocks"] == 4
    assert len({b["id"] for b in result["blocks"]}) == 4
    result["blocks"][2]["heading_path"].append("changed")
    assert payload == before


def test_source_and_version_changes_invalidate_anchor():
    payload = source()
    anchor = build_document_map(payload, version_id=4)["blocks"][0]["id"]
    with pytest.raises(ValueError, match="source_changed"):
        read_map_block(payload, version_id=5, block_id=anchor)
    payload["blocks"][3]["text"] += "新内容"
    with pytest.raises(ValueError, match="source_changed"):
        read_map_block(payload, version_id=4, block_id=anchor)


def test_processing_metadata_does_not_change_evidence_identity():
    payload = source()
    before = build_document_map(payload, version_id=4)
    payload["metadata"] = {"processing_status": "done", "confidence": float("nan")}
    assert build_document_map(payload, version_id=4) == before


@pytest.mark.parametrize("field,value", [("page", True), ("page", "1"),
                                         ("paragraph_index", -1), ("type", "invented")])
def test_invalid_locations_and_block_types(field, value):
    payload = source()
    payload["blocks"][0][field] = value
    with pytest.raises(ValueError):
        build_document_map(payload, version_id=4)


def test_large_table_pages_reconstruct_exact_source():
    payload = source()
    anchor = build_document_map(payload, version_id=4)["blocks"][2]["id"]
    offset, parts = 0, []
    while True:
        result = read_map_block(payload, version_id=4, block_id=anchor, offset=offset, max_chars=3)
        assert result["partial"]
        parts.append(result["text"])
        offset = result["next_offset"]
        if offset is None:
            break
    assert "".join(parts) == payload["blocks"][2]["text"]


@pytest.mark.parametrize("kwargs", [{"offset": True}, {"offset": -1}, {"max_chars": True},
                                      {"max_chars": 12001}, {"max_chars": 0}])
def test_strict_pagination(kwargs):
    with pytest.raises(ValueError):
        read_map_block(source(), version_id=4, block_id="invalid", **kwargs)


@pytest.mark.parametrize("budget", [1, 3, 6, 6000])
def test_windows_cover_every_character_without_truncation(budget):
    payload = source()
    payload["blocks"][2]["text"] *= 1300
    before = copy.deepcopy(payload)
    windows = plan_source_windows(payload, version_id=4, max_chars=budget, max_segments=2)
    seen = {index: "" for index in range(4)}
    for window in windows:
        assert 0 < window["source_chars"] <= budget
        assert 0 < len(window["segments"]) <= 2
        for segment in window["segments"]:
            index = segment["block_index"]
            assert segment["start"] == len(seen[index])
            seen[index] += payload["blocks"][index]["text"][segment["start"]:segment["stop"]]
    assert [seen[i] for i in range(4)] == [b["text"] for b in payload["blocks"]]
    assert payload == before


def test_empty_document_has_no_analysis_windows():
    assert plan_source_windows({"blocks": []}, version_id=1) == []


@pytest.mark.parametrize("kwargs", [{"max_chars": True}, {"max_chars": 0},
                                      {"max_segments": True}, {"max_segments": 0}])
def test_invalid_window_limits(kwargs):
    with pytest.raises(ValueError):
        plan_source_windows(source(), version_id=1, **kwargs)
