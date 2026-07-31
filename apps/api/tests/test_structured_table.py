import pytest

from apps.api.models.table_rows import StructuredTableRow
from apps.api.parsers.base import Block, StructuredContent
from apps.api.services.structured_table import (
    TableDataset,
    execute_query_plan,
    extract_table_row_payloads,
    is_structured_table_question,
    validate_query_plan,
)


def _dataset() -> TableDataset:
    return TableDataset(
        document_id=8,
        document_version_id=12,
        title="采购表",
        sheet_name="明细",
        region_index=1,
        columns=("品类", "金额", "完成率"),
        row_count=4,
    )


def _rows() -> list[StructuredTableRow]:
    values = [
        (2, {"品类": "水果", "金额": "￥1,200元", "完成率": "25.0%（原始值：0.25）"}),
        (3, {"品类": "水果", "金额": "800", "完成率": "50%（原始值：0.5）"}),
        (4, {"品类": "蔬菜", "金额": "500", "完成率": "75%（原始值：0.75）"}),
        (5, {"品类": "蔬菜", "金额": "无", "完成率": None}),
    ]
    return [
        StructuredTableRow(
            document_id=8,
            document_version_id=12,
            sheet_name="明细",
            region_index=1,
            row_number=row_number,
            values=row_values,
        )
        for row_number, row_values in values
    ]


def test_extract_rows_preserves_missing_cells_and_row_coordinates():
    structured = StructuredContent(
        document_type="xlsx",
        blocks=[
            Block(
                type="table",
                text="行 2｜品类=水果｜金额=12\n行 3｜金额=9｜备注=含｜符号",
                heading_path=["明细", "数据区域 2"],
                extra={
                    "sheet_name": "明细",
                    "region_index": 2,
                    "column_names": ["品类", "金额", "备注"],
                },
            )
        ],
    )

    rows = extract_table_row_payloads(structured)

    assert rows[0]["row_number"] == 2
    assert rows[0]["values"] == {"品类": "水果", "金额": "12", "备注": None}
    assert rows[1]["region_index"] == 2
    assert rows[1]["values"] == {"品类": None, "金额": "9", "备注": "含｜符号"}


def test_executor_filters_groups_and_sums_exactly():
    dataset = _dataset()
    plan = validate_query_plan(
        {
            "document_id": 8,
            "sheet_name": "明细",
            "region_index": 1,
            "filters": [{"column": "品类", "operator": "eq", "value": "水果"}],
            "group_by": ["品类"],
            "metric": "sum",
            "metric_column": "金额",
            "sort_by": "metric",
            "sort_order": "desc",
            "limit": 10,
        },
        [dataset],
    )

    result = execute_query_plan(dataset, _rows(), plan)

    assert result.rows == [{"品类": "水果", "metric": 2000, "matched_rows": 2}]
    assert result.matched_row_count == 2
    assert result.source_row_start == 2
    assert result.source_row_end == 3


def test_executor_uses_original_percent_value_and_reports_invalid_numbers():
    dataset = _dataset()
    plan = validate_query_plan(
        {
            "document_id": 8,
            "sheet_name": "明细",
            "region_index": 1,
            "filters": [],
            "group_by": ["品类"],
            "metric": "avg",
            "metric_column": "完成率",
            "sort_by": "metric",
            "sort_order": "desc",
            "limit": 10,
        },
        [dataset],
    )

    result = execute_query_plan(dataset, _rows(), plan)

    assert result.rows[0]["品类"] == "蔬菜"
    assert result.rows[0]["metric"] == 0.75
    assert result.rows[1]["metric"] == 0.375
    assert result.warnings == ["1 行的 完成率 不是有效数字，已忽略"]


def test_plan_rejects_unknown_columns_and_code_fields():
    base = {
        "document_id": 8,
        "sheet_name": "明细",
        "region_index": 1,
        "filters": [],
        "group_by": [],
        "metric": "sum",
        "metric_column": "不存在",
        "sort_order": "desc",
        "limit": 10,
    }
    with pytest.raises(ValueError, match="统计列不存在"):
        validate_query_plan(base, [_dataset()])
    with pytest.raises(ValueError, match="不允许的字段"):
        validate_query_plan({**base, "sql": "drop table documents"}, [_dataset()])


def test_plan_accepts_model_null_limit_as_safe_default():
    plan = validate_query_plan(
        {
            "document_id": 8,
            "sheet_name": "明细",
            "region_index": 1,
            "filters": [],
            "group_by": [],
            "metric": "count",
            "metric_column": None,
            "sort_by": "metric",
            "sort_order": "desc",
            "limit": None,
        },
        [_dataset()],
    )
    assert plan.limit == 20


def test_plan_normalizes_model_empty_optional_fields():
    plan = validate_query_plan(
        {
            "document_id": 8,
            "sheet_name": "明细",
            "region_index": 1,
            "filters": [],
            "group_by": [],
            "metric": "rows",
            "metric_column": "",
            "sort_by": "",
            "sort_order": "",
            "limit": 0,
        },
        [_dataset()],
    )
    assert plan.metric_column is None
    assert plan.sort_by is None
    assert plan.sort_order == "desc"
    assert plan.limit == 20


def test_executor_can_return_filtered_and_ranked_detail_rows():
    dataset = _dataset()
    plan = validate_query_plan(
        {
            "document_id": 8,
            "sheet_name": "明细",
            "region_index": 1,
            "filters": [{"column": "品类", "operator": "eq", "value": "水果"}],
            "group_by": [],
            "metric": "rows",
            "metric_column": None,
            "sort_by": "金额",
            "sort_order": "desc",
            "limit": 1,
        },
        [dataset],
    )

    result = execute_query_plan(dataset, _rows(), plan)

    assert result.matched_row_count == 2
    assert result.rows == [
        {
            "row_number": 2,
            "品类": "水果",
            "金额": "￥1,200元",
            "完成率": "25.0%（原始值：0.25）",
        }
    ]


@pytest.mark.parametrize(
    "question",
    [
        "2026-06-26 这一天提供了哪些项目？",
        "列出状态为完成的记录",
        "这个部门包含什么人员？",
        "哪几笔交易超过了一万元？",
    ],
)
def test_generic_table_detail_questions_use_structured_path(question):
    assert is_structured_table_question(question) is True
