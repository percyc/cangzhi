import asyncio

import pytest

from apps.api.models.table_rows import StructuredTableRow
from apps.api.parsers.base import Block, StructuredContent
from apps.api.services.structured_table import (
    TableDataset,
    TableQueryResult,
    _literal_detail_plan_is_complete,
    _literal_matches_for_question,
    execute_query_plan,
    extract_table_row_payloads,
    is_structured_table_question,
    try_structured_table_query,
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


def test_plan_clamps_large_model_limit_instead_of_falling_back():
    plan = validate_query_plan(
        {
            "document_id": 8,
            "sheet_name": "明细",
            "region_index": 1,
            "filters": [],
            "group_by": [],
            "metric": "rows",
            "metric_column": None,
            "sort_by": None,
            "sort_order": None,
            "limit": 100,
        },
        [_dataset()],
    )
    assert plan.limit == 50


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


def test_fallback_executor_direct_child_filter_excludes_deeper_descendants():
    dataset = TableDataset(
        document_id=8,
        document_version_id=12,
        title="区域人口",
        sheet_name="指标",
        region_index=1,
        columns=("行政区划", "人口"),
        row_count=3,
    )
    rows = [
        StructuredTableRow(
            document_id=8,
            document_version_id=12,
            sheet_name="指标",
            region_index=1,
            row_number=index,
            values={"行政区划": path, "人口": population},
        )
        for index, (path, population) in enumerate(
            [
                ("广东省-赤坎区-中华街道", "10"),
                ("广东省-赤坎区-寸金街道", "20"),
                ("广东省-赤坎区-寸金街道-西苑社区", "99"),
            ],
            start=2,
        )
    ]
    plan = validate_query_plan(
        {
            "document_id": 8,
            "sheet_name": "指标",
            "region_index": 1,
            "filters": [
                {
                    "column": "行政区划",
                    "operator": "direct_child_of",
                    "value": "赤坎区",
                }
            ],
            "group_by": [],
            "metric": "sum",
            "metric_column": "人口",
            "sort_order": "desc",
            "limit": 10,
        },
        [dataset],
    )

    result = execute_query_plan(dataset, rows, plan)
    assert result.matched_row_count == 2
    assert result.rows == [{"metric": 30, "matched_rows": 2}]


@pytest.mark.parametrize(
    "question",
    [
        "2026-06-26 这一天提供了哪些项目？",
        "列出状态为完成的记录",
        "这个部门包含什么人员？",
        "哪几笔交易超过了一万元？",
        "6月23日的早餐有什么菜？",
        "仓库里有啥物料？",
        "今年华东销售趋势如何？",
        "比较各部门预算和实际支出的差异",
        "查看最近一个月的交易明细",
    ],
)
def test_generic_table_detail_questions_use_structured_path(question):
    assert is_structured_table_question(question) is True


def test_detail_filter_inference_uses_literal_cell_values_not_business_columns():
    rows = [
        StructuredTableRow(
            document_id=8,
            document_version_id=12,
            sheet_name="任意数据",
            region_index=1,
            row_number=2,
            values={
                "字段甲": "2026-06-26",
                "字段乙": "晚餐",
                "字段丙": "霸王花瘦肉汤",
                "数值": "26",
            },
        )
    ]

    matches = _literal_matches_for_question(
        "2026-06-26 这一天的晚餐有哪些项目？",
        rows,
        ["字段甲", "字段乙", "字段丙", "数值"],
    )

    assert matches == (("字段甲", "2026-06-26"), ("字段乙", "晚餐"))


def test_literal_inference_resolves_unique_yearless_date_alias():
    rows = [
        StructuredTableRow(
            document_id=8,
            document_version_id=12,
            sheet_name="任意数据",
            region_index=1,
            row_number=index,
            values={"字段甲": value, "字段乙": "早餐"},
        )
        for index, value in enumerate(
            ["2026-06-22", "2026-06-23", "2026-06-24"], start=2
        )
    ]

    matches = _literal_matches_for_question(
        "6月23日的早餐有什么菜？",
        rows,
        ["字段甲", "字段乙"],
    )

    assert matches == (("字段甲", "2026-06-23"), ("字段乙", "早餐"))


def test_literal_inference_does_not_guess_year_for_ambiguous_date_alias():
    rows = [
        StructuredTableRow(
            document_id=8,
            document_version_id=12,
            sheet_name="任意数据",
            region_index=1,
            row_number=index,
            values={"字段甲": value, "字段乙": "早餐"},
        )
        for index, value in enumerate(["2025-06-23", "2026-06-23"], start=2)
    ]

    matches = _literal_matches_for_question(
        "6月23日的早餐有哪些菜？",
        rows,
        ["字段甲", "字段乙"],
    )

    assert matches == (("字段乙", "早餐"),)


def test_literal_inference_accepts_one_character_value_when_bound_to_column():
    rows = [
        StructuredTableRow(
            document_id=8,
            document_version_id=12,
            sheet_name="任意数据",
            region_index=1,
            row_number=index,
            values={"是否更新": value, "指标": f"指标 {index}"},
        )
        for index, value in enumerate(["是", "否", "否"], start=2)
    ]

    matches = _literal_matches_for_question(
        "是否更新为否的记录有多少条？",
        rows,
        ["是否更新", "指标"],
    )

    assert matches == (("是否更新", "否"),)


def test_weak_detail_phrase_needs_complete_literal_constraints():
    incidental = TableDataset(
        document_id=8,
        document_version_id=12,
        title="项目跟踪",
        sheet_name="明细",
        region_index=1,
        columns=("状态", "结论"),
        row_count=10,
        semantic_summary="项目状态和结论跟踪表",
        literal_matches=(("状态", "完成"),),
    )
    complete = TableDataset(
        document_id=9,
        document_version_id=13,
        title="排班",
        sheet_name="明细",
        region_index=1,
        columns=("日期", "班次", "人员"),
        row_count=10,
        semantic_summary="每日班次和人员排班",
        literal_matches=(("日期", "2026-06-23"), ("班次", "早班")),
    )

    assert _literal_detail_plan_is_complete("这份完成表有什么结论？", incidental) is False
    assert _literal_detail_plan_is_complete("列出状态为完成的记录", incidental) is True
    assert _literal_detail_plan_is_complete("6月23日早班有什么人？", complete) is True


def test_dataset_prompt_exposes_semantic_summary_for_relevance_routing():
    dataset = TableDataset(
        document_id=8,
        document_version_id=12,
        title="物料表",
        sheet_name="库存",
        region_index=1,
        columns=("物料", "数量"),
        row_count=20,
        semantic_summary="仓库物料与实时库存数量",
    )

    assert dataset.to_prompt_dict()["semantic_summary"] == "仓库物料与实时库存数量"


def test_forced_semantic_query_refines_empty_parent_lookup_to_direct_children(
    monkeypatch,
):
    import apps.api.services.structured_table as module

    dataset = TableDataset(
        document_id=34,
        document_version_id=35,
        title="区域指标",
        sheet_name="指标",
        region_index=1,
        columns=("行政区划", "指标名称", "数据期", "指标值"),
        row_count=100,
        dataset_id=14,
    )

    class Provider:
        def __init__(self):
            self.responses = [
                {
                    "relevant": True,
                    "document_id": 34,
                    "sheet_name": "指标",
                    "region_index": 1,
                    "filters": [
                        {
                            "column": "行政区划",
                            "operator": "eq",
                            "value": "广东省-赤坎区",
                        }
                    ],
                    "group_by": [],
                    "metric": "rows",
                    "metric_column": None,
                    "sort_by": None,
                    "sort_order": "asc",
                    "limit": 1,
                },
                {
                    "relevant": True,
                    "document_id": 34,
                    "sheet_name": "指标",
                    "region_index": 1,
                    "filters": [
                        {
                            "column": "行政区划",
                            "operator": "direct_child_of",
                            "value": "赤坎区",
                        }
                    ],
                    "group_by": [],
                    "metric": "sum",
                    "metric_column": "指标值",
                    "sort_by": "metric",
                    "sort_order": "desc",
                    "limit": 20,
                },
            ]

        def generate_json(self, **_kwargs):
            return self.responses.pop(0)

    async def load_datasets(_db, _document_ids, *, question):
        return [dataset]

    async def execute_plan(_db, selected_dataset, plan):
        if plan.filters[0].operator == "eq":
            return TableQueryResult(
                dataset=selected_dataset,
                plan=plan,
                rows=[],
                source_rows=[],
                source_row_start=None,
                source_row_end=None,
                matched_row_count=0,
                summary="zero",
            )
        return TableQueryResult(
            dataset=selected_dataset,
            plan=plan,
            rows=[{"metric": 369174, "matched_rows": 8}],
            source_rows=list(range(1, 9)),
            source_row_start=1,
            source_row_end=8,
            matched_row_count=8,
            summary="sum",
        )

    monkeypatch.setattr(module, "_load_datasets", load_datasets)
    monkeypatch.setattr(module, "_execute_dataset_plan", execute_plan)

    result = asyncio.run(
        try_structured_table_query(
            object(),
            provider=Provider(),
            question="2023年赤坎区常住人口",
            document_ids=[34],
            force=True,
        )
    )

    assert result is not None
    assert result.plan.filters[0].operator == "direct_child_of"
    assert result.rows == [{"metric": 369174, "matched_rows": 8}]
