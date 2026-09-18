import copy
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from apps.api.parsers.base import Block, StructuredContent
from apps.api.services.chunker import build_chunk_specs
from apps.api.services.chunking_candidate import source_fingerprint, MAX_BLOCKS
from apps.api.services.chunking_adaptive import build_adaptive_candidate, evaluate_windows


def fragmented(kind="txt", count=6):
    return StructuredContent(kind, [Block("paragraph", f"事实 {i}。", [], page=1) for i in range(count)]).to_dict()


def baseline(payload):
    return [asdict(s) for s in build_chunk_specs(payload, content_hash_seed=source_fingerprint(payload))]


def model(calls):
    def generate(**kwargs):
        blocks = json.loads(kwargs["prompt"])["blocks"]
        calls.append(blocks)
        return {"ends": [blocks[-1]["id"]]}
    return SimpleNamespace(generate_json=generate)


@pytest.mark.parametrize("kind", ["pdf", "doc", "docx", "markdown", "txt", "html", "note"])
def test_all_text_formats_evaluate_without_mutating_source(kind):
    source = fragmented(kind)
    before = copy.deepcopy(source)
    calls = []
    result = build_adaptive_candidate(source, provider=model(calls))
    assert result["accepted_windows"] == 1
    assert source == before
    parents = [s for s in result["specs"] if s["role"] == "parent"]
    assert "\n\n".join(s["content"] for s in parents) == "\n\n".join(b["text"] for b in source["blocks"])


def test_clear_structure_does_not_call_model_and_keeps_exact_baseline():
    source = StructuredContent("docx", [Block("heading", "说明", [], level=1),
        Block("paragraph", "清楚的正文。" * 30, ["说明"])]).to_dict()
    calls = []
    result = build_adaptive_candidate(source, provider=model(calls))
    assert not calls and result["decision"] == "no_risk"
    assert result["specs"] == baseline(source)


@pytest.mark.parametrize("mode", ["no_provider", "budget", "invalid", "exception"])
def test_no_valid_ai_keeps_whole_document_baseline(mode):
    source = fragmented(count=70)
    def generate(**_):
        if mode == "exception":
            raise RuntimeError("SECRET_SHOULD_NOT_ESCAPE")
        return {"ends": [-1]}
    result = build_adaptive_candidate(source,
        provider=None if mode == "no_provider" else SimpleNamespace(generate_json=generate),
        max_calls=0 if mode == "budget" else 2)
    assert result["specs"] == baseline(source)
    assert result["assisted_blocks"] == 0
    assert "SECRET_SHOULD_NOT_ESCAPE" not in json.dumps(result)


def test_later_high_risk_window_precedes_earlier_fragments_and_offsets_are_global():
    blocks = [Block("paragraph", f"短句{i}。", [], page=1) for i in range(6)]
    blocks += [Block("paragraph", "表前说明", [], page=2),
               Block("table", "项目 | 数量\n甲 | 3", [], page=2),
               Block("paragraph", "表后说明", [], page=2)]
    source = StructuredContent("pdf", blocks).to_dict()
    calls = []
    result = build_adaptive_candidate(source, provider=model(calls), max_calls=1)
    assert calls[0][0]["id"] == 6
    assert result["groups"][0] == {"start": 0, "end": 5, "mode": "rules"}
    parents = [s for s in result["specs"] if s["role"] == "parent"]
    assert parents[1]["source_start"] == sum(len(b.text) + 2 for b in blocks[:6])
    assert "\n\n".join(s["content"] for s in parents) == "\n\n".join(b.text for b in blocks)
    identifiers = {s["external_id"] for s in parents}
    assert all(s["parent_external_id"] in identifiers for s in result["specs"] if s["role"] == "child")


@pytest.mark.parametrize("kind", ["xls", "xlsx", "database_table"])
def test_dataset_route_preserved(kind):
    source = fragmented(kind)
    calls = []
    result = build_adaptive_candidate(source, provider=model(calls))
    assert result["skipped_dataset"] and not calls
    assert result["specs"] == baseline(source)


def test_code_and_oversize_blocks_are_not_sent():
    blocks = [Block("paragraph", f"短段{i}", []) for i in range(6)]
    blocks += [Block("code_block", "print('safe')", []), Block("paragraph", "长" * 7000, [])]
    source = StructuredContent("markdown", blocks).to_dict()
    calls = []
    result = build_adaptive_candidate(source, provider=model(calls))
    assert not calls and result["specs"] == baseline(source)
    assert "code_preserved" in result["reasons"]
    assert "oversize_block_requires_separate_strategy" in result["reasons"]


def test_model_cannot_split_table_explanations():
    blocks = [Block("paragraph", "说明", []), Block("table", "甲 | 3", []), Block("paragraph", "注释", [])]
    source = StructuredContent("docx", blocks).to_dict()
    result = build_adaptive_candidate(source, provider=SimpleNamespace(generate_json=lambda **_: {"ends": [0, 2]}))
    assert result["failed_windows"] == 1
    assert result["specs"] == baseline(source)


def test_budget_is_hard_capped_and_coverage_is_honest():
    source = fragmented(count=32 * 12)
    calls = []
    result = build_adaptive_candidate(source, provider=model(calls), max_calls=100)
    assert len(calls) == 8 and result["unassisted_eligible_windows"] == 4
    assert result["coverage"] < 1
    with pytest.raises(ValueError, match="too_many_blocks"):
        build_adaptive_candidate(fragmented(count=MAX_BLOCKS + 1))


def test_selected_window_retains_enclosing_heading_path():
    blocks = [Block("heading", "章节", ["章节"], page=1, level=1),
              Block("paragraph", "正常说明。" * 30, ["章节"], page=1)]
    blocks += [Block("paragraph", f"后续短段{i}。", ["章节"], page=2) for i in range(6)]
    result = build_adaptive_candidate(StructuredContent("docx", blocks).to_dict(), provider=model([]))
    enhanced = [s for s in result["specs"] if s["extra"].get("boundary_mode") == "ai"]
    assert enhanced and all(s["heading_path"] == ["章节"] for s in enhanced)


def test_valid_model_boundaries_that_increase_tiny_fragments_are_rejected():
    source = fragmented()
    result = build_adaptive_candidate(source, provider=SimpleNamespace(generate_json=lambda **_: {"ends": list(range(6))}))
    assert result["decision"] == "quality_fallback"
    assert result["calls"] == 1 and result["validated_windows"] == 1
    assert result["accepted_windows"] == 0 and result["assisted_blocks"] == 0
    assert result["specs"] == baseline(source)
