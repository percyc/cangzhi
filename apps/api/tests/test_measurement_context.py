import asyncio
from pathlib import Path

import duckdb
import pytest

from apps.api.models.datasets import DatasetField
from apps.api.models.table_rows import StructuredTableRow
from apps.api.services.dataset_execution import (
    _execute_parquet_query,
    validate_safe_query,
)
from apps.api.services.measurement_context import (
    ColumnStats,
    evaluate_measure_context,
    is_context_column,
)
from apps.api.services.structured_table import (
    TableDataset,
    TableQueryPlan,
    execute_query_plan,
)


@pytest.mark.parametrize(
    ("values", "metric", "groups", "state"),
    [
        (["人", "人"], "sum", [], "known"),
        ([" 人 ", "人"], "sum", [], "known"),
        (["USD", "CNY"], "sum", [], "blocked"),
        (["kg", None], "avg", [], "blocked"),
        (["kg", ""], "max", [], "blocked"),
        ([None, ""], "min", [], "unknown"),
        (["USD", "CNY"], "sum", ["单位"], "grouped"),
        (["kg", None], "sum", ["单位"], "grouped"),
        (["USD", "CNY"], "count", [], "not_applicable"),
        (["USD", "CNY"], "count_distinct", [], "not_applicable"),
        (["USD", "CNY"], "rows", [], "not_applicable"),
        (["x" * 80, "x" * 80], "sum", [], "unknown"),
    ],
)
def test_backends_verify_same_matched_units(
    tmp_path: Path, values, metric, groups, state
):
    fields = [
        DatasetField(name=n, inferred_type="number" if i == 0 else "text", position=i)
        for i, n in enumerate(["值", "单位"])
    ]
    path = tmp_path / "rows.parquet"
    connection = duckdb.connect()
    connection.execute(
        'CREATE TABLE data (__row_number BIGINT, "值" VARCHAR, "单位" VARCHAR)'
    )
    connection.executemany(
        "INSERT INTO data VALUES (?, ?, ?)",
        [(i + 1, str(i + 2), unit) for i, unit in enumerate(values)],
    )
    connection.execute("COPY data TO ? (FORMAT PARQUET)", [str(path)])
    connection.close()
    raw = {"metric": metric, "metric_column": "值", "group_by": groups}
    query = validate_safe_query(raw, [f.name for f in fields])
    plain = _execute_parquet_query(path, query, fields)
    assert "measure_context" not in plain
    sql = _execute_parquet_query(path, query, fields, include_measure_context=True)
    dataset = TableDataset(
        document_id=1,
        document_version_id=1,
        title="synthetic",
        sheet_name="data",
        region_index=1,
        columns=("值", "单位"),
        row_count=2,
    )
    rows = [
        StructuredTableRow(row_number=i + 1, values={"值": str(i + 2), "单位": unit})
        for i, unit in enumerate(values)
    ]
    plan = TableQueryPlan(
        document_id=1,
        sheet_name="data",
        region_index=1,
        metric=metric,
        metric_column="值",
        group_by=tuple(groups),
    )
    fallback = execute_query_plan(dataset, rows, plan, include_measure_context=True)
    assert sql["measure_context"]["state"] == fallback.measure_context["state"] == state
    if state == "known":
        assert sql["measure_context"]["columns"][0]["unit"] == "人"
    if state == "blocked":
        assert fallback.rows == []


@pytest.mark.parametrize(
    "name", ["单位名称", "unit_price", "amount_unit", "金额(单位)", "地区"]
)
def test_does_not_guess_context_association(name):
    assert not is_context_column(name)


def test_complete_distinct_count_not_bounded_sample_controls_verdict():
    verdict = evaluate_measure_context(
        metric="sum",
        matched_row_count=100,
        stats_by_column={"currency": ColumnStats("currency", 100, 7, 0, ("USD",))},
    )
    assert verdict.blocked


def test_missing_candidates_empty_and_excess_are_not_confirmed():
    assert (
        evaluate_measure_context(metric="sum", matched_row_count=1).state == "unknown"
    )
    assert (
        evaluate_measure_context(metric="sum", matched_row_count=0).state == "unknown"
    )
    assert evaluate_measure_context(
        metric="sum", matched_row_count=1, candidates_truncated=True
    ).blocked


@pytest.mark.parametrize(("metric", "sort_by"), [("sum", "值"), ("rows", "metric")])
def test_planner_rejects_sorting_that_executor_would_reject(metric, sort_by):
    from apps.api.services.structured_table import validate_query_plan

    dataset = TableDataset(
        document_id=1,
        document_version_id=1,
        title="synthetic",
        sheet_name="data",
        region_index=1,
        columns=("值", "单位"),
        row_count=2,
    )
    with pytest.raises(ValueError, match="排序字段无效"):
        validate_query_plan(
            {
                "document_id": 1,
                "sheet_name": "data",
                "metric": metric,
                "metric_column": "值",
                "sort_by": sort_by,
            },
            [dataset],
        )


@pytest.mark.parametrize("narrow", [False, True])
def test_unit_refinement_is_bounded_and_cannot_drop_conflicting_rows(
    monkeypatch, narrow
):
    import apps.api.services.structured_table as module

    dataset = TableDataset(
        document_id=1,
        document_version_id=1,
        title="合成金额",
        sheet_name="data",
        region_index=1,
        columns=("值", "币种"),
        row_count=2,
    )
    rows = [
        StructuredTableRow(row_number=i + 1, values={"值": "10", "币种": unit})
        for i, unit in enumerate(["USD", "CNY"])
    ]

    async def load(*args, **kwargs):
        return [dataset]

    async def execute(db, ds, plan, **kwargs):
        return execute_query_plan(ds, rows, plan, **kwargs)

    class Provider:
        calls = 0

        def generate_json(self, **kwargs):
            self.calls += 1
            assert self.calls <= 2
            plan = {
                "relevant": True,
                "document_id": 1,
                "sheet_name": "data",
                "region_index": 1,
                "metric": "sum",
                "metric_column": "值",
            }
            if self.calls == 2:
                assert "单位诊断" in kwargs["prompt"]
                plan.update(
                    {"filters": [{"column": "币种", "operator": "eq", "value": "USD"}]}
                    if narrow
                    else {"group_by": ["币种"]}
                )
            return plan

    monkeypatch.setattr(module, "_load_datasets", load)
    monkeypatch.setattr(module, "_execute_dataset_plan", execute)
    provider = Provider()
    result = asyncio.run(
        module.try_structured_table_query(
            object(),
            provider=provider,
            question="金额合计",
            document_ids=[1],
            force=True,
        )
    )
    assert provider.calls == 2
    assert result.matched_row_count == 2
    assert result.measure_context["state"] == ("blocked" if narrow else "grouped")
