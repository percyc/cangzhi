from types import SimpleNamespace

import pytest

from apps.api.models.datasets import DatasetField, KnowledgeDataset
from apps.api.services.dataset_semantics import (
    MAX_DESCRIPTION_CHARS,
    enrich_dataset_fields,
    validate_field_suggestions,
)


def test_validate_field_suggestions_rejects_unknown_and_bounds_values():
    result = validate_field_suggestions(
        {
            "fields": [
                {
                    "name": "金额",
                    "description": " 本期 含税金额 ",
                    "unit": "元",
                    "aliases": ["含税金额", "含税金额", "金额", 3],
                    "confidence": 4,
                },
                {
                    "name": "不存在",
                    "description": "不能写入",
                    "unit": None,
                    "aliases": [],
                    "confidence": 1,
                },
            ]
        },
        ["金额"],
    )

    assert result == {
        "金额": {
            "description": "本期 含税金额",
            "unit": "元",
            "aliases": ["含税金额"],
            "confidence": 1.0,
        }
    }


def test_validate_field_suggestions_requires_description_and_truncates():
    result = validate_field_suggestions(
        {
            "fields": [
                {
                    "name": "代码",
                    "description": "x" * (MAX_DESCRIPTION_CHARS + 50),
                    "unit": None,
                    "aliases": [],
                    "confidence": "bad",
                },
                {
                    "name": "状态",
                    "description": " ",
                    "unit": None,
                    "aliases": [],
                    "confidence": 0.8,
                },
            ]
        },
        ["代码", "状态"],
    )

    assert len(result["代码"]["description"]) == MAX_DESCRIPTION_CHARS
    assert result["代码"]["confidence"] == 0.0
    assert "状态" not in result


def test_enrich_dataset_fields_updates_only_semantic_metadata():
    calls = []

    def generate_json(**kwargs):
        calls.append(kwargs)
        return {
            "fields": [
                {
                    "name": "amt1",
                    "description": "可能表示当前记录的金额，具体口径需结合来源确认",
                    "unit": None,
                    "aliases": ["金额"],
                    "confidence": 0.42,
                }
            ]
        }

    provider = SimpleNamespace(name="fake", _model="unit-test", generate_json=generate_json)
    dataset = KnowledgeDataset(
        id=9,
        document_id=3,
        document_version_id=7,
        name="orders",
        sheet_name="Sheet1",
        region_index=1,
        row_count=10,
        column_count=1,
        profile={"quality": {"completeness": 1}},
    )
    field = DatasetField(
        id=2,
        dataset_id=9,
        position=0,
        name="amt1",
        inferred_type="number",
        null_count=0,
        distinct_count=10,
        sample_values=["12.5", "30"],
        statistics={"min": 12.5, "max": 30},
    )

    result = enrich_dataset_fields(
        provider, dataset, [field], document_title="订单快照"
    )

    assert result.updated == 1
    assert result.calls == 1
    assert field.name == "amt1"
    assert field.inferred_type == "number"
    assert field.sample_values == ["12.5", "30"]
    assert field.description.startswith("可能表示")
    assert field.aliases == ["金额"]
    assert field.semantic_source == "ai"
    assert field.semantic_confidence == pytest.approx(0.42)
    assert dataset.profile["quality"] == {"completeness": 1}
    assert dataset.profile["field_semantics"]["updated_fields"] == 1
    assert "只读事实" in calls[0]["system"]
