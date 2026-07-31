"""Safe, row-aware querying for parsed spreadsheet documents."""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from ..ai import AIProvider, AIProviderError
from ..models.table_rows import StructuredTableRow
from ..parsers.base import StructuredContent

_ROW_RE = re.compile(r"^行\s+(\d+)｜(.*)$")
_ORIGINAL_VALUE_RE = re.compile(r"（原始值：([^）]+)）$")
_NUMBER_CLEAN_RE = re.compile(r"[,，\s￥¥$元]")
_STRUCTURED_INTENT_RE = re.compile(
    r"(多少|几个|数量|总数|合计|总计|总和|求和|平均|均值|最大|最小|最高|最低|"
    r"分组|各自|分别|排名|排行|前\s*\d+|后\s*\d+|筛选|过滤|占比|百分比|"
    r"count|sum|average|avg|max|min|group|rank|top\s*\d+)",
    re.IGNORECASE,
)

ALLOWED_OPERATORS = {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "in"}
ALLOWED_METRICS = {"rows", "count", "count_distinct", "sum", "avg", "min", "max"}
ALLOWED_SORT_ORDERS = {"asc", "desc"}
MAX_QUERY_ROWS = 100_000
MAX_RESULT_ROWS = 50
MAX_GROUP_COLUMNS = 3
MAX_FILTERS = 8
MAX_REFERENCE_ROWS = 200


@dataclass(frozen=True)
class TableDataset:
    document_id: int
    document_version_id: int
    title: str
    sheet_name: str
    region_index: int
    columns: tuple[str, ...]
    row_count: int
    samples: tuple[dict[str, Any], ...] = ()

    @property
    def key(self) -> tuple[int, str, int]:
        return (self.document_id, self.sheet_name, self.region_index)

    def to_prompt_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "title": self.title,
            "sheet_name": self.sheet_name,
            "region_index": self.region_index,
            "columns": list(self.columns),
            "row_count": self.row_count,
            "samples": list(self.samples),
        }


@dataclass(frozen=True)
class TableFilter:
    column: str
    operator: str
    value: Any


@dataclass(frozen=True)
class TableQueryPlan:
    document_id: int
    sheet_name: str
    region_index: int
    filters: tuple[TableFilter, ...] = ()
    group_by: tuple[str, ...] = ()
    metric: str = "count"
    metric_column: str | None = None
    sort_by: str | None = None
    sort_order: str = "desc"
    limit: int = 20


@dataclass
class TableQueryResult:
    dataset: TableDataset
    plan: TableQueryPlan
    rows: list[dict[str, Any]]
    source_rows: list[int]
    source_row_start: int | None
    source_row_end: int | None
    matched_row_count: int
    summary: str
    warnings: list[str] = field(default_factory=list)


def is_structured_table_question(question: str) -> bool:
    return bool(_STRUCTURED_INTENT_RE.search(question or ""))


def _parse_semantic_row(
    line: str,
    column_names: Sequence[str],
) -> tuple[int, dict[str, str | None]] | None:
    match = _ROW_RE.match(line.strip())
    if match is None:
        return None
    row_number = int(match.group(1))
    remainder = match.group(2)
    values: dict[str, str | None] = {column: None for column in column_names}
    alternatives = "|".join(
        re.escape(column) for column in sorted(column_names, key=len, reverse=True)
    )
    markers = list(re.finditer(rf"(?:^|｜)({alternatives})=", remainder))
    for index, marker in enumerate(markers):
        value_start = marker.end()
        value_end = (
            markers[index + 1].start() if index + 1 < len(markers) else len(remainder)
        )
        values[marker.group(1)] = remainder[value_start:value_end] or None
    if not any(value is not None for value in values.values()):
        return None
    return row_number, values


def extract_table_row_payloads(
    structured_content: StructuredContent,
) -> list[dict[str, Any]]:
    """Recover row records from v2 spreadsheet table blocks."""

    payloads: list[dict[str, Any]] = []
    for block in structured_content.blocks:
        if block.type != "table":
            continue
        extra = block.extra or {}
        columns = [str(item) for item in extra.get("column_names") or [] if item]
        sheet_name = str(extra.get("sheet_name") or "工作表")
        region_index = int(extra.get("region_index") or 1)
        if not columns:
            continue
        for line in (block.text or "").splitlines():
            parsed = _parse_semantic_row(line, columns)
            if parsed is None:
                continue
            row_number, values = parsed
            payloads.append(
                {
                    "sheet_name": sheet_name,
                    "region_index": region_index,
                    "row_number": row_number,
                    "values": values,
                }
            )
    return payloads


def replace_version_table_rows(
    session: Session,
    *,
    document_id: int,
    document_version_id: int,
    structured_content: StructuredContent,
) -> int:
    """Idempotently replace persisted rows inside the worker transaction."""

    session.execute(
        delete(StructuredTableRow).where(
            StructuredTableRow.document_version_id == document_version_id
        )
    )
    payloads = extract_table_row_payloads(structured_content)
    session.add_all(
        [
            StructuredTableRow(
                document_id=document_id,
                document_version_id=document_version_id,
                **payload,
            )
            for payload in payloads
        ]
    )
    return len(payloads)


def _numeric(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
            return number if number.is_finite() else None
        except InvalidOperation:
            return None
    text = str(value).strip()
    original = _ORIGINAL_VALUE_RE.search(text)
    if original:
        text = original.group(1)
    is_percent = text.endswith("%")
    text = _NUMBER_CLEAN_RE.sub("", text.removesuffix("%"))
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number / Decimal(100) if is_percent and original is None else number


def _comparable_pair(actual: Any, expected: Any) -> tuple[Any, Any]:
    actual_number = _numeric(actual)
    expected_number = _numeric(expected)
    if actual_number is not None and expected_number is not None:
        return actual_number, expected_number
    return str(actual or "").casefold(), str(expected or "").casefold()


def _matches(values: dict[str, Any], condition: TableFilter) -> bool:
    actual = values.get(condition.column)
    expected = condition.value
    if condition.operator == "contains":
        return str(expected).casefold() in str(actual or "").casefold()
    if condition.operator == "in":
        if not isinstance(expected, list):
            return False
        for item in expected:
            left, right = _comparable_pair(actual, item)
            if left == right:
                return True
        return False
    left, right = _comparable_pair(actual, expected)
    if condition.operator == "eq":
        return left == right
    if condition.operator == "ne":
        return left != right
    if condition.operator == "gt":
        return left > right
    if condition.operator == "gte":
        return left >= right
    if condition.operator == "lt":
        return left < right
    if condition.operator == "lte":
        return left <= right
    return False


def validate_query_plan(
    raw: dict[str, Any], datasets: Sequence[TableDataset]
) -> TableQueryPlan:
    if not isinstance(raw, dict):
        raise TypeError("查询计划必须是 JSON 对象")
    allowed_keys = {
        "document_id",
        "sheet_name",
        "region_index",
        "filters",
        "group_by",
        "metric",
        "metric_column",
        "sort_by",
        "sort_order",
        "limit",
    }
    if set(raw) - allowed_keys:
        raise ValueError("查询计划包含不允许的字段")
    try:
        document_id = int(raw["document_id"])
        sheet_name = str(raw["sheet_name"])
        region_index = int(raw.get("region_index", 1))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("查询计划缺少有效的数据集定位") from exc
    dataset = next(
        (
            item
            for item in datasets
            if item.key == (document_id, sheet_name, region_index)
        ),
        None,
    )
    if dataset is None:
        raise ValueError("查询计划选择了不存在的数据集")
    columns = set(dataset.columns)
    raw_filters = raw.get("filters") or []
    if not isinstance(raw_filters, list) or len(raw_filters) > MAX_FILTERS:
        raise ValueError("筛选条件数量无效")
    filters: list[TableFilter] = []
    for item in raw_filters:
        if not isinstance(item, dict) or set(item) != {"column", "operator", "value"}:
            raise ValueError("筛选条件格式无效")
        column = str(item["column"])
        operator = str(item["operator"])
        if column not in columns or operator not in ALLOWED_OPERATORS:
            raise ValueError("筛选条件使用了无效列或运算符")
        value = item["value"]
        scalar_types = (str, int, float, bool, type(None))
        if operator == "in":
            if (
                not isinstance(value, list)
                or len(value) > 100
                or any(not isinstance(entry, scalar_types) for entry in value)
            ):
                raise ValueError("in 筛选值必须是不超过 100 项的简单数组")
        elif not isinstance(value, scalar_types):
            raise ValueError("筛选值必须是简单类型")
        filters.append(TableFilter(column, operator, value))
    group_by_raw = raw.get("group_by") or []
    if not isinstance(group_by_raw, list) or len(group_by_raw) > MAX_GROUP_COLUMNS:
        raise ValueError("分组列数量无效")
    group_by = tuple(str(item) for item in group_by_raw)
    if any(column not in columns for column in group_by):
        raise ValueError("分组列不存在")
    metric = str(raw.get("metric") or "count")
    if metric not in ALLOWED_METRICS:
        raise ValueError("统计方式不在白名单中")
    metric_column = raw.get("metric_column")
    metric_column = str(metric_column) if metric_column is not None else None
    if metric not in {"rows", "count"} and (
        metric_column is None or metric_column not in columns
    ):
        raise ValueError("统计列不存在")
    if metric == "rows" and group_by:
        raise ValueError("返回明细行时不能同时分组")
    sort_by = raw.get("sort_by")
    sort_by = str(sort_by) if sort_by is not None else None
    result_columns = columns | set(group_by) | {"metric", "matched_rows"}
    if sort_by is not None and sort_by not in result_columns:
        raise ValueError("排序字段无效")
    sort_order = str(raw.get("sort_order") or "desc")
    if sort_order not in ALLOWED_SORT_ORDERS:
        raise ValueError("排序方向无效")
    limit = raw.get("limit", 20)
    if limit is None:
        limit = 20
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_RESULT_ROWS
    ):
        raise ValueError(f"返回数量必须在 1 到 {MAX_RESULT_ROWS} 之间")
    return TableQueryPlan(
        document_id=document_id,
        sheet_name=sheet_name,
        region_index=region_index,
        filters=tuple(filters),
        group_by=group_by,
        metric=metric,
        metric_column=metric_column,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
    )


def _aggregate(
    metric: str, rows: Sequence[StructuredTableRow], column: str | None
) -> tuple[Any, list[str]]:
    if metric == "count":
        return len(rows), []
    if metric == "count_distinct":
        return len(
            {
                str(row.values.get(column))
                for row in rows
                if row.values.get(column) not in (None, "")
            }
        ), []
    numbers = [_numeric(row.values.get(column)) for row in rows]
    valid = [number for number in numbers if number is not None]
    warnings = (
        []
        if len(valid) == len(rows)
        else [f"{len(rows) - len(valid)} 行的 {column} 不是有效数字，已忽略"]
    )
    if not valid:
        return None, warnings
    if metric == "sum":
        value = sum(valid, Decimal(0))
    elif metric == "avg":
        value = sum(valid, Decimal(0)) / len(valid)
    elif metric == "min":
        value = min(valid)
    else:
        value = max(valid)
    return float(value) if value != value.to_integral_value() else int(value), warnings


def execute_query_plan(
    dataset: TableDataset,
    rows: Sequence[StructuredTableRow],
    plan: TableQueryPlan,
) -> TableQueryResult:
    filtered = [
        row for row in rows if all(_matches(row.values, item) for item in plan.filters)
    ]
    if plan.metric == "rows":
        detail_rows = [
            {"row_number": row.row_number, **dict(row.values or {})} for row in filtered
        ]
        if plan.sort_by:
            detail_rows.sort(
                key=lambda item: _safe_sort_value(item.get(plan.sort_by)),
                reverse=plan.sort_order == "desc",
            )
        detail_rows = detail_rows[: plan.limit]
        summary = json.dumps(
            {
                "数据集": (
                    f"{dataset.title} / {dataset.sheet_name} / "
                    f"区域 {dataset.region_index}"
                ),
                "筛选后行数": len(filtered),
                "返回明细": detail_rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        return TableQueryResult(
            dataset=dataset,
            plan=plan,
            rows=detail_rows,
            source_rows=[int(item["row_number"]) for item in detail_rows],
            source_row_start=min((row.row_number for row in filtered), default=None),
            source_row_end=max((row.row_number for row in filtered), default=None),
            matched_row_count=len(filtered),
            summary=summary,
        )
    groups: dict[tuple[Any, ...], list[StructuredTableRow]] = defaultdict(list)
    if plan.group_by:
        for row in filtered:
            groups[tuple(row.values.get(column) for column in plan.group_by)].append(
                row
            )
    else:
        groups[()] = filtered
    result_rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    if plan.metric in {"sum", "avg", "min", "max"}:
        invalid_count = sum(
            1
            for row in filtered
            if _numeric(row.values.get(plan.metric_column)) is None
        )
        if invalid_count:
            warnings.append(
                f"{invalid_count} 行的 {plan.metric_column} 不是有效数字，已忽略"
            )
    source_rows: list[int] = []
    for key, group_rows in groups.items():
        metric_value, _metric_warnings = _aggregate(
            plan.metric, group_rows, plan.metric_column
        )
        item = {column: value for column, value in zip(plan.group_by, key)}
        item.update({"metric": metric_value, "matched_rows": len(group_rows)})
        result_rows.append(item)
        source_rows.extend(row.row_number for row in group_rows[:MAX_REFERENCE_ROWS])
    sort_by = plan.sort_by or "metric"
    result_rows.sort(
        key=lambda item: _safe_sort_value(item.get(sort_by)),
        reverse=plan.sort_order == "desc",
    )
    result_rows = result_rows[: plan.limit]
    summary = json.dumps(
        {
            "数据集": (
                f"{dataset.title} / {dataset.sheet_name} / 区域 {dataset.region_index}"
            ),
            "筛选后行数": len(filtered),
            "统计方式": plan.metric,
            "统计列": plan.metric_column,
            "分组列": list(plan.group_by),
            "计算结果": result_rows,
            "提示": sorted(set(warnings)),
        },
        ensure_ascii=False,
        indent=2,
    )
    return TableQueryResult(
        dataset=dataset,
        plan=plan,
        rows=result_rows,
        source_rows=sorted(set(source_rows)),
        source_row_start=min((row.row_number for row in filtered), default=None),
        source_row_end=max((row.row_number for row in filtered), default=None),
        matched_row_count=len(filtered),
        summary=summary,
        warnings=sorted(set(warnings)),
    )


def _safe_sort_value(value: Any) -> tuple[int, Any]:
    if value is None:
        return (0, "")
    numeric = _numeric(value)
    if numeric is not None:
        return (2, numeric)
    return (1, str(value).casefold())


async def _load_datasets(
    db: AsyncSession, document_ids: Sequence[int]
) -> list[TableDataset]:
    from ..models.documents import Document, DocumentVersion

    if not document_ids:
        return []
    result = await db.execute(
        select(StructuredTableRow, Document.title)
        .join(Document, Document.id == StructuredTableRow.document_id)
        .join(
            DocumentVersion,
            DocumentVersion.id == StructuredTableRow.document_version_id,
        )
        .where(
            StructuredTableRow.document_id.in_(list(document_ids)),
            Document.current_version_id == StructuredTableRow.document_version_id,
            Document.is_deleted.is_(False),
        )
        .order_by(
            StructuredTableRow.document_id,
            StructuredTableRow.sheet_name,
            StructuredTableRow.region_index,
            StructuredTableRow.row_number,
        )
        .limit(MAX_QUERY_ROWS + 1)
    )
    loaded_rows = result.all()
    if len(loaded_rows) > MAX_QUERY_ROWS:
        # Never return a plausible-looking partial aggregate. A future
        # DuckDB/Polars backend will handle datasets above this boundary.
        return []
    grouped: dict[tuple[int, str, int], list[tuple[StructuredTableRow, str]]] = (
        defaultdict(list)
    )
    for row, title in loaded_rows:
        grouped[(row.document_id, row.sheet_name, row.region_index)].append(
            (row, title)
        )
    datasets: list[TableDataset] = []
    for (document_id, sheet_name, region_index), items in grouped.items():
        first, title = items[0]
        columns = tuple(str(column) for column in (first.values or {}))
        datasets.append(
            TableDataset(
                document_id=document_id,
                document_version_id=first.document_version_id,
                title=title,
                sheet_name=sheet_name,
                region_index=region_index,
                columns=columns,
                row_count=len(items),
                samples=tuple(dict(row.values or {}) for row, _ in items[:3]),
            )
        )
    return datasets


_PLAN_SYSTEM = """你是藏知的表格查询规划器。
只返回 JSON 对象，不生成 SQL、Python 或解释文字。
你只能从给定数据集和列名中选择。计划字段固定为：
document_id、sheet_name、region_index、filters、group_by、metric、
metric_column、sort_by、sort_order、limit。
filters 每项固定为 column/operator/value，operator 仅可用
eq/ne/gt/gte/lt/lte/contains/in。
metric 仅可用 rows/count/count_distinct/sum/avg/min/max。
用户要查看、筛选或排序具体明细时用 rows，group_by=[]。
没有分组但需要总计时 group_by=[]。
排名时设置 group_by、sort_by=metric、sort_order=desc 和 limit。
不要臆造列名或数据集。"""


async def try_structured_table_query(
    db: AsyncSession,
    *,
    provider: AIProvider,
    question: str,
    document_ids: Sequence[int],
) -> TableQueryResult | None:
    if not is_structured_table_question(question):
        return None
    datasets = await _load_datasets(db, document_ids)
    if not datasets:
        return None
    prompt = json.dumps(
        {
            "问题": question,
            "可用数据集": [dataset.to_prompt_dict() for dataset in datasets[:12]],
        },
        ensure_ascii=False,
    )
    try:
        raw_plan = await asyncio.to_thread(
            provider.generate_json,
            system=_PLAN_SYSTEM,
            prompt=prompt,
        )
        plan = validate_query_plan(raw_plan, datasets)
    except (AIProviderError, ValueError, TypeError, KeyError):
        return None
    dataset = next(
        item
        for item in datasets
        if item.key == (plan.document_id, plan.sheet_name, plan.region_index)
    )
    rows = list(
        (
            await db.scalars(
                select(StructuredTableRow)
                .where(
                    StructuredTableRow.document_version_id
                    == dataset.document_version_id,
                    StructuredTableRow.sheet_name == dataset.sheet_name,
                    StructuredTableRow.region_index == dataset.region_index,
                )
                .order_by(StructuredTableRow.row_number)
                .limit(MAX_QUERY_ROWS)
            )
        ).all()
    )
    return execute_query_plan(dataset, rows, plan)
