import copy
import json
from types import SimpleNamespace

import pytest

from apps.api.parsers.base import Block, StructuredContent
from apps.api.services.chunking_candidate import build_candidate, _validate_ends


def payload(count=6, kind="txt"):
    return StructuredContent(kind, [Block("paragraph", f"原文块 {i}：事实与条件。", [], page=1, paragraph_index=i) for i in range(count)]).to_dict()


def provider(ends):
    return SimpleNamespace(generate_json=lambda **_: {"ends": ends})


def test_ai_changes_boundaries_without_rewriting_or_mutating_source():
    source = payload()
    original = copy.deepcopy(source)
    result = build_candidate(source, provider=provider([1, 3, 5]))
    assert source == original
    assert result["accepted_windows"] == 1
    assert result["assisted_blocks"] == 6
    assert result["baseline"]["children"] == 1
    assert result["candidate"]["children"] == 3
    parents = [s for s in result["specs"] if s["role"] == "parent"]
    assert "\n\n".join(s["content"] for s in parents) == "\n\n".join(b["text"] for b in source["blocks"])
    assert result["activated"] is False


@pytest.mark.parametrize("ends", [[], [1], [5, 3], [-1, 5], [6], [True, 5], ["5"], [1, 1, 5], [0, 9, 5]])
def test_invalid_output_falls_back_without_losing_blocks(ends):
    result = build_candidate(payload(), provider=provider(ends))
    assert result["failed_windows"] == 1
    assert result["assisted_blocks"] == 0
    assert result["groups"] == [{"start": 0, "end": 5, "mode": "rules"}]


def test_output_cannot_supply_new_text():
    fake = SimpleNamespace(generate_json=lambda **_: {"ends": [5], "text": "改写原文"})
    result = build_candidate(payload(), provider=fake)
    assert result["failed_windows"] == 1
    assert "改写原文" not in json.dumps(result, ensure_ascii=False)


def test_table_and_neighbors_not_separated():
    blocks = [Block("paragraph", "解释", []), Block("table", "列一|列二\nA|B", []), Block("paragraph", "注释", [])]
    with pytest.raises(ValueError, match="table_context_split"):
        _validate_ends({"ends": [0, 2]}, 0, 3, blocks)
    assert _validate_ends({"ends": [2]}, 0, 3, blocks) == [2]


def test_budget_and_uncovered_windows_are_explicit():
    calls = []
    def generate(**kwargs):
        items = json.loads(kwargs["prompt"])["blocks"]
        calls.append(items)
        return {"ends": [items[-1]["id"]]}
    result = build_candidate(payload(100), provider=SimpleNamespace(generate_json=generate), max_calls=2)
    assert len(calls) == result["calls"] == 2
    assert result["assisted_blocks"] == 64
    assert result["total_blocks"] == 100
    assert result["groups"][-1]["mode"] == "rules"


def test_provider_error_is_sanitized():
    def fail(**_):
        raise RuntimeError("credential-and-private-text")
    result = build_candidate(payload(), provider=SimpleNamespace(generate_json=fail))
    assert "credential-and-private-text" not in json.dumps(result)
    assert result["failed_windows"] == 1


def test_pdf_candidate_demotes_false_headings_but_preserves_source():
    source = payload(6, "pdf")
    for b in source["blocks"]:
        b.update(type="heading", text="ＧＢ １４７６２ ２００８", level=1)
    before = copy.deepcopy(source)
    result = build_candidate(source)
    assert source == before
    assert result["candidate"]["parents"] < result["baseline"]["parents"]
    assert sum(s["content"].count("ＧＢ") for s in result["specs"] if s["role"] == "parent") == 6


@pytest.mark.parametrize("kind", ["xlsx", "xls", "database_table"])
def test_datasets_never_call_boundary_model(kind):
    def fail(**_):
        raise AssertionError("not allowed")
    result = build_candidate(payload(kind=kind), provider=SimpleNamespace(generate_json=fail))
    assert result["skipped_dataset"] and result["calls"] == 0


def test_large_block_not_sent_or_truncated():
    source = payload(2)
    source["blocks"][0]["text"] = "长" * 7000
    result = build_candidate(source, provider=provider([1]))
    assert result["calls"] == 0
    assert sum(s["content"].count("长") for s in result["specs"] if s["role"] == "parent") == 7000


def test_window_budget_does_not_sever_table_context():
    from apps.api.services.chunking_candidate import _windows
    blocks = [Block("paragraph", "x" * 5900, []),
              Block("table", "table" * 30, []),
              Block("paragraph", "notes", []),
              Block("paragraph", "next", [])]
    assert list(_windows(blocks)) == [(0, 3), (3, 4)]
    result = build_candidate(StructuredContent("docx", blocks).to_dict(), provider=provider([3]))
    assert result["calls"] == 0


def test_model_cannot_separate_a_heading_from_its_body():
    blocks = [Block("heading", "Chapter 1 Introduction", [], level=1),
              Block("paragraph", "body", [])]
    with pytest.raises(ValueError, match="heading_context_split"):
        _validate_ends({"ends": [0, 1]}, 0, 2, blocks)
