"""Safe, row-aware querying for parsed spreadsheet documents."""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from ..ai import AIProvider, AIProviderError
from ..models.datasets import DatasetArtifact, DatasetField, KnowledgeDataset
from ..models.table_rows import StructuredTableRow
from ..parsers.base import StructuredContent

_ROW_RE = re.compile(r"^行\s+(\d+)｜(.*)$")
_ORIGINAL_VALUE_RE = re.compile(r"（原始值：([^）]+)）$")
_NUMBER_CLEAN_RE = re.compile(r"[,，\s￥¥$元]")
_STRUCTURED_INTENT_RE = re.compile(
    r"(多少|几个|数量|总数|合计|总计|总和|求和|平均|均值|最大|最小|最高|最低|"
    r"哪些|哪几|列出|罗列|包含什么|包含哪些|提供了什么|提供了哪些|"
    r"有什么|都有什么|有哪些|有哪几|有啥|"
    r"分组|各自|分别|排名|排行|前\s*\d+|后\s*\d+|筛选|过滤|占比|百分比|"
    r"count|sum|average|avg|max|min|group|rank|top\s*\d+)",
    re.IGNORECASE,
)
_DETAIL_INTENT_RE = re.compile(
    r"(哪些|哪几|列出|罗列|包含什么|包含哪些|"
    r"提供了什么|提供了哪些|有什么|都有什么|"
    r"有哪些|有哪几|有啥)",
    re.IGNORECASE,
)
_COUNT_INTENT_RE = re.compile(r"(多少|几个|数量|总数|count)", re.IGNORECASE)
_DATE_VALUE_RE = re.compile(
    r"^(?P<year>\d{4})[-/.\u5e74](?P<month>\d{1,2})[-/.\u6708]"
    r"(?P<day>\d{1,2})(?:\u65e5)?(?:[T\s].*)?$"
)
_DATE_QUERY_RE = re.compile(
    r"(?<!\d)(?:(?:\d{4})[-/.\u5e74])?(?:0?[1-9]|1[0-2])"
    r"[-/.\u6708](?:0?[1-9]|[12]\d|3[01])\u65e5?(?!\d)"
)

ALLOWED_OPERATORS = {
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "starts_with",
    "ends_with",
    "direct_child_of",
    "in",
}
ALLOWED_METRICS = {"rows", "count", "count_distinct", "sum", "avg", "min", "max"}
ALLOWED_SORT_ORDERS = {"asc", "desc"}
MAX_QUERY_ROWS = 150_000
MAX_RESULT_ROWS = 50
MAX_GROUP_COLUMNS = 3
MAX_FILTERS = 8
MAX_REFERENCE_ROWS = 200
MAX_PROFILE_DISTINCT_VALUES = 500
MAX_PROFILE_SAMPLES = 8
MAX_DATASET_DISCOVERY_DOCUMENTS = 12


@dataclass(frozen=True)
class TableDataset:
    document_id: int
    document_version_id: int
    title: str
    sheet_name: str
    region_index: int
    columns: tuple[str, ...]
    row_count: int
    semantic_summary: str = ""
    samples: tuple[dict[str, Any], ...] = ()
    literal_matches: tuple[tuple[str, str], ...] = ()
    records: tuple[StructuredTableRow, ...] = field(
        default=(), repr=False, compare=False
    )
    dataset_id: int | None = None

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
            "semantic_summary": self.semantic_summary,
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


_HIERARCHY_SEPARATORS = frozenset("-/＞>")


def _is_direct_child_path(actual: Any, parent: Any) -> bool:
    """Match exactly one segment below a parent in a path-like text field."""

    text = str(actual or "").strip()
    target = str(parent or "").strip()
    if not text or not target:
        return False
    start = 0
    while True:
        index = text.find(target, start)
        if index < 0:
            return False
        before_ok = index == 0 or text[index - 1] in _HIERARCHY_SEPARATORS
        suffix_start = index + len(target)
        after_ok = (
            suffix_start < len(text)
            and text[suffix_start] in _HIERARCHY_SEPARATORS
        )
        if before_ok and after_ok:
            child = text[suffix_start + 1 :]
            return bool(child) and not any(
                separator in child for separator in _HIERARCHY_SEPARATORS
            )
        start = index + 1


async def candidate_dataset_document_ids(
    db: AsyncSession,
    *,
    filters: dict[str, Any],
    limit: int = MAX_DATASET_DISCOVERY_DOCUMENTS,
) -> list[int]:
    """Return in-scope dataset documents when chunk discovery has no hit."""

    from ..models.documents import Document
    from .search import _apply_filters

    stmt = (
        select(Document.id)
        .join(KnowledgeDataset, KnowledgeDataset.document_id == Document.id)
        .where(
            Document.is_deleted.is_(False),
            Document.current_version_id == KnowledgeDataset.document_version_id,
        )
        .distinct()
        .order_by(Document.id.desc())
        .limit(max(1, min(limit, MAX_DATASET_DISCOVERY_DOCUMENTS)))
    )
    stmt = _apply_filters(stmt, filters)
    return [int(document_id) for document_id in (await db.scalars(stmt)).all()]


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
        if extra.get("dataset_eligible") is False:
            continue
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
    existing_dataset_ids = list(
        session.scalars(
            select(KnowledgeDataset.id).where(
                KnowledgeDataset.document_version_id == document_version_id
            )
        ).all()
    )
    if existing_dataset_ids:
        session.execute(
            delete(DatasetField).where(
                DatasetField.dataset_id.in_(existing_dataset_ids)
            )
        )
    session.execute(
        delete(KnowledgeDataset).where(
            KnowledgeDataset.document_version_id == document_version_id
        )
    )

    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for payload in payloads:
        grouped[(payload["sheet_name"], payload["region_index"])].append(payload)
    region_metadata = _region_metadata(structured_content)
    for (sheet_name, region_index), rows in grouped.items():
        columns = tuple(str(item) for item in rows[0]["values"])
        region = region_metadata.get((sheet_name, region_index), {})
        dataset = KnowledgeDataset(
            document_id=document_id,
            document_version_id=document_version_id,
            name=(
                sheet_name
                if region_index == 1
                else f"{sheet_name} · 数据区域 {region_index}"
            ),
            sheet_name=sheet_name,
            region_index=region_index,
            dataset_kind="table",
            status="ready",
            row_count=len(rows),
            column_count=len(columns),
            header_row=region.get("header_row"),
            source_row_start=min(row["row_number"] for row in rows),
            source_row_end=max(row["row_number"] for row in rows),
            profile={
                "profile_version": 1,
                "quality": _dataset_quality(rows, columns),
            },
        )
        session.add(dataset)
        session.flush()
        session.add_all(_profile_dataset_fields(dataset.id, rows, columns))
        session.add_all(
            [
                StructuredTableRow(
                    document_id=document_id,
                    document_version_id=document_version_id,
                    dataset_id=dataset.id,
                    **payload,
                )
                for payload in rows
            ]
        )
    return len(payloads)


def _region_metadata(
    structured_content: StructuredContent,
) -> dict[tuple[str, int], dict[str, Any]]:
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for block in structured_content.blocks:
        if block.type != "table":
            continue
        extra = block.extra or {}
        key = (
            str(extra.get("sheet_name") or "工作表"),
            int(extra.get("region_index") or 1),
        )
        result.setdefault(key, dict(extra))
    return result


def _semantic_role(column: str) -> str | None:
    lowered = column.casefold()
    patterns = (
        ("time", r"日期|时间|年月|数据期|统计期|时期|年度|月份|date|time|period"),
        (
            "measure",
            (
                r"金额|数量|单价|合计|总额|比例|比率|完成率|占比|"
                r"amount|price|count|total|ratio|rate"
            ),
        ),
        ("identifier", r"编号|序号|代码|编码|id|code"),
        ("status", r"状态|是否|标志|结果|status|flag"),
        ("category", r"类型|类别|分类|部门|地区|城市|category|type|department"),
    )
    return next(
        (role for role, pattern in patterns if re.search(pattern, lowered)), None
    )


def _profile_dataset_fields(
    dataset_id: int,
    rows: Sequence[dict[str, Any]],
    columns: Sequence[str],
) -> list[DatasetField]:
    fields: list[DatasetField] = []
    for position, column in enumerate(columns):
        semantic_role = _semantic_role(column)
        null_count = 0
        non_empty_count = 0
        numeric_count = 0
        date_count = 0
        boolean_count = 0
        samples: list[str] = []
        distinct: set[str] = set()
        distinct_overflow = False
        numeric_min: Decimal | None = None
        numeric_max: Decimal | None = None
        for row in rows:
            value = row["values"].get(column)
            if value in (None, ""):
                null_count += 1
                continue
            text_value = str(value).strip()
            non_empty_count += 1
            if len(samples) < MAX_PROFILE_SAMPLES and text_value not in samples:
                samples.append(text_value)
            if len(distinct) < MAX_PROFILE_DISTINCT_VALUES:
                distinct.add(text_value)
            elif text_value not in distinct:
                distinct_overflow = True
            numeric = _numeric(text_value)
            if numeric is not None:
                numeric_count += 1
                numeric_min = (
                    numeric if numeric_min is None else min(numeric_min, numeric)
                )
                numeric_max = (
                    numeric if numeric_max is None else max(numeric_max, numeric)
                )
            if _date_parts(text_value) is not None:
                date_count += 1
            if text_value.casefold() in {"true", "false", "yes", "no", "是", "否"}:
                boolean_count += 1
        threshold = max(1, int(non_empty_count * 0.9))
        if semantic_role == "identifier":
            inferred_type = "identifier"
        elif semantic_role == "time" and date_count < threshold:
            inferred_type = "text"
        elif date_count >= threshold:
            inferred_type = "date"
        elif boolean_count >= threshold:
            inferred_type = "boolean"
        elif numeric_count >= threshold:
            inferred_type = "number"
        else:
            inferred_type = "text"
        statistics: dict[str, Any] = {
            "non_empty_count": non_empty_count,
            "distinct_count_capped": distinct_overflow,
        }
        if (
            inferred_type == "number"
            and numeric_min is not None
            and numeric_max is not None
        ):
            statistics.update({"min": float(numeric_min), "max": float(numeric_max)})
        fields.append(
            DatasetField(
                dataset_id=dataset_id,
                position=position,
                name=column,
                inferred_type=inferred_type,
                semantic_role=semantic_role,
                confidence=(
                    round(
                        max(numeric_count, date_count, boolean_count) / non_empty_count,
                        3,
                    )
                    if non_empty_count
                    else None
                ),
                null_count=null_count,
                distinct_count=None if distinct_overflow else len(distinct),
                sample_values=samples,
                statistics=statistics,
            )
        )
    return fields


def _dataset_quality(
    rows: Sequence[dict[str, Any]], columns: Sequence[str]
) -> dict[str, Any]:
    total_cells = len(rows) * len(columns)
    empty_cells = sum(
        value in (None, "") for row in rows for value in row["values"].values()
    )
    return {
        "total_cells": total_cells,
        "empty_cells": empty_cells,
        "completeness": (
            round((total_cells - empty_cells) / total_cells, 4) if total_cells else 1.0
        ),
    }


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
    if condition.operator == "starts_with":
        return str(actual or "").casefold().startswith(str(expected).casefold())
    if condition.operator == "ends_with":
        return str(actual or "").casefold().endswith(str(expected).casefold())
    if condition.operator == "direct_child_of":
        return _is_direct_child_path(actual, expected)
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
        "relevant",
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
    metric_column = str(metric_column).strip() if metric_column is not None else None
    metric_column = metric_column or None
    if metric not in {"rows", "count"} and (
        metric_column is None or metric_column not in columns
    ):
        raise ValueError("统计列不存在")
    if metric == "rows" and group_by:
        raise ValueError("返回明细行时不能同时分组")
    sort_by = raw.get("sort_by")
    sort_by = str(sort_by).strip() if sort_by is not None else None
    sort_by = sort_by or None
    result_columns = columns | set(group_by) | {"metric", "matched_rows"}
    if sort_by is not None and sort_by not in result_columns:
        raise ValueError("排序字段无效")
    sort_order = str(raw.get("sort_order") or "desc")
    if sort_order not in ALLOWED_SORT_ORDERS:
        raise ValueError("排序方向无效")
    limit = raw.get("limit", 20)
    if limit is None or (
        isinstance(limit, int) and not isinstance(limit, bool) and limit == 0
    ):
        limit = 20
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("返回数量必须是正整数")
    limit = min(limit, MAX_RESULT_ROWS)
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
        warnings = (
            [f"结果共 {len(filtered)} 行，仅返回前 {plan.limit} 行"]
            if len(filtered) > plan.limit
            else []
        )
        summary = json.dumps(
            {
                "数据集": (
                    f"{dataset.title} / {dataset.sheet_name} / "
                    f"区域 {dataset.region_index}"
                ),
                "筛选后行数": len(filtered),
                "返回明细": detail_rows,
                "提示": warnings,
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
            warnings=warnings,
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


def _date_parts(value: str) -> tuple[int, int, int] | None:
    match = _DATE_VALUE_RE.match(value.strip())
    if match is None:
        return None
    parts = tuple(int(match.group(key)) for key in ("year", "month", "day"))
    try:
        date(*parts)
    except ValueError:
        return None
    return parts


def _date_aliases(value: str) -> tuple[str, ...]:
    parts = _date_parts(value)
    if parts is None:
        return ()
    year, month, day = parts
    return (
        f"{year}年{month}月{day}日",
        f"{year}-{month:02d}-{day:02d}",
        f"{year}/{month:02d}/{day:02d}",
        f"{year}.{month:02d}.{day:02d}",
        f"{month}月{day}日",
        f"{month:02d}月{day:02d}日",
        f"{month}-{day}",
        f"{month:02d}-{day:02d}",
        f"{month}/{day}",
        f"{month:02d}/{day:02d}",
        f"{month}.{day}",
        f"{month:02d}.{day:02d}",
    )


def _question_contains_alias(question: str, alias: str) -> bool:
    return bool(re.search(rf"(?<!\d){re.escape(alias)}(?!\d)", question))


def _literal_matches_for_question(
    question: str,
    rows: Sequence[StructuredTableRow],
    columns: Sequence[str],
) -> tuple[tuple[str, str], ...]:
    normalized_question = question.casefold()
    matches: list[tuple[str, str]] = []
    for column in columns:
        candidates = {
            str(row.values.get(column)).strip()
            for row in rows
            if row.values.get(column) not in (None, "")
        }
        # One-character categorical values such as 是/否 are too ambiguous for
        # a global substring match. They are safe when the question explicitly
        # binds the value to its column, for example “是否更新为否”.
        explicitly_bound = [
            value
            for value in candidates
            if 1 <= len(value) <= 80
            and not value.replace(".", "", 1).isdigit()
            and re.search(
                rf"{re.escape(column)}\s*(?:为|是|等于|属于|：|:|=)\s*"
                rf"{re.escape(value)}(?:\s|的|、|，|。|？|\?|$)",
                question,
                re.IGNORECASE,
            )
        ]
        if explicitly_bound:
            matches.append((column, max(explicitly_bound, key=len)))
            continue
        matched = [
            value
            for value in candidates
            if 2 <= len(value) <= 80
            and not value.replace(".", "", 1).isdigit()
            and value.casefold() in normalized_question
        ]
        if matched:
            matches.append((column, max(matched, key=len)))
            continue

        # Spreadsheet parsers persist dates in a canonical form such as
        # 2026-06-23, while people commonly omit the year. Resolve such an
        # alias only when it identifies exactly one value in this dataset;
        # this avoids silently choosing a year in multi-year tables.
        alias_values: dict[str, set[str]] = defaultdict(set)
        for value in candidates:
            for alias in _date_aliases(value):
                alias_values[alias.casefold()].add(value)
        date_matches = [
            (alias, next(iter(values)))
            for alias, values in alias_values.items()
            if len(values) == 1 and _question_contains_alias(normalized_question, alias)
        ]
        if date_matches:
            _, resolved_value = max(date_matches, key=lambda item: len(item[0]))
            matches.append((column, resolved_value))
    return tuple(matches)


def _literal_detail_plan_is_complete(
    question: str,
    dataset: TableDataset,
) -> bool:
    """Decide whether literals alone safely express the user's filters.

    A weak phrase such as "有什么" must not turn one incidental cell value
    into a broad detail query. Two independently resolved values are sufficient;
    one value is accepted only when the question explicitly binds it as a
    condition (for example "状态为完成").
    """

    matches = dataset.literal_matches
    if not matches:
        return False
    if _DATE_QUERY_RE.search(question) and not any(
        _date_parts(value) is not None or bool(_DATE_QUERY_RE.fullmatch(value))
        for _, value in matches
    ):
        return False
    if len(matches) >= 2:
        return True
    value = matches[0][1]
    escaped = re.escape(value)
    return bool(
        re.search(
            rf"(?:为|是|等于|属于|：|:|=)\s*{escaped}"
            rf"(?:\s|的|、|，|。|？|\?|$)",
            question,
            re.IGNORECASE,
        )
    )


async def _load_datasets(
    db: AsyncSession,
    document_ids: Sequence[int],
    *,
    question: str = "",
) -> list[TableDataset]:
    from ..models.documents import Document
    from ..models.taxonomy import DocumentSummary

    if not document_ids:
        return []
    result = await db.execute(
        select(KnowledgeDataset, Document.title, DocumentSummary.summary)
        .join(Document, Document.id == KnowledgeDataset.document_id)
        .where(
            KnowledgeDataset.document_id.in_(list(document_ids)),
            Document.current_version_id == KnowledgeDataset.document_version_id,
            Document.is_deleted.is_(False),
        )
        .outerjoin(
            DocumentSummary,
            DocumentSummary.document_version_id == KnowledgeDataset.document_version_id,
        )
        .order_by(
            KnowledgeDataset.document_id,
            KnowledgeDataset.sheet_name,
            KnowledgeDataset.region_index,
        )
    )
    catalogs = result.all()
    if not catalogs:
        # Compatibility for pre-0020 rows and lightweight test fixtures. New
        # ingestion always creates first-class dataset catalog records.
        legacy = await db.execute(
            select(StructuredTableRow, Document.title, DocumentSummary.summary)
            .join(Document, Document.id == StructuredTableRow.document_id)
            .where(
                StructuredTableRow.document_id.in_(list(document_ids)),
                Document.current_version_id == StructuredTableRow.document_version_id,
                Document.is_deleted.is_(False),
            )
            .outerjoin(
                DocumentSummary,
                DocumentSummary.document_version_id
                == StructuredTableRow.document_version_id,
            )
            .order_by(
                StructuredTableRow.document_id,
                StructuredTableRow.sheet_name,
                StructuredTableRow.region_index,
                StructuredTableRow.row_number,
            )
            .limit(MAX_QUERY_ROWS + 1)
        )
        legacy_rows = legacy.all()
        if len(legacy_rows) > MAX_QUERY_ROWS:
            return []
        grouped: dict[
            tuple[int, str, int],
            list[tuple[StructuredTableRow, str, str]],
        ] = defaultdict(list)
        for row, title, semantic_summary in legacy_rows:
            grouped[(row.document_id, row.sheet_name, row.region_index)].append(
                (row, title, semantic_summary or "")
            )
        return [
            TableDataset(
                document_id=document_id,
                document_version_id=items[0][0].document_version_id,
                title=items[0][1],
                sheet_name=sheet_name,
                region_index=region_index,
                columns=tuple(str(name) for name in (items[0][0].values or {})),
                row_count=len(items),
                semantic_summary=items[0][2],
                samples=tuple(dict(row.values or {}) for row, _, _ in items[:3]),
                literal_matches=_literal_matches_for_question(
                    question,
                    [row for row, _, _ in items],
                    tuple(str(name) for name in (items[0][0].values or {})),
                ),
                records=tuple(row for row, _, _ in items),
            )
            for (document_id, sheet_name, region_index), items in grouped.items()
        ]
    catalog_ids = [catalog.id for catalog, _, _ in catalogs]
    artifact_dataset_ids = (
        set(
            (
                await db.scalars(
                    select(DatasetArtifact.dataset_id).where(
                        DatasetArtifact.dataset_id.in_(catalog_ids),
                        DatasetArtifact.is_active.is_(True),
                        DatasetArtifact.status == "ready",
                    )
                )
            ).all()
        )
        if catalog_ids
        else set()
    )
    datasets: list[TableDataset] = []
    for catalog, title, semantic_summary in catalogs:
        fields = list(
            (
                await db.scalars(
                    select(DatasetField)
                    .where(DatasetField.dataset_id == catalog.id)
                    .order_by(DatasetField.position)
                )
            ).all()
        )
        records = list(
            (
                await db.scalars(
                    select(StructuredTableRow)
                    .where(StructuredTableRow.dataset_id == catalog.id)
                    .order_by(StructuredTableRow.row_number)
                    .limit(
                        4 if catalog.id in artifact_dataset_ids else MAX_QUERY_ROWS + 1
                    )
                )
            ).all()
        )
        complete_records = (
            []
            if catalog.id in artifact_dataset_ids
            else records
            if len(records) <= MAX_QUERY_ROWS
            else []
        )
        artifact_literals: tuple[tuple[str, str], ...] = ()
        if catalog.id in artifact_dataset_ids and question:
            from .dataset_execution import discover_dataset_literals

            artifact_literals = await discover_dataset_literals(
                db, catalog.id, question
            )
        samples = tuple(dict(row.values or {}) for row in records[:3])
        columns = tuple(field.name for field in fields) or tuple(
            str(column) for column in ((records[0].values or {}) if records else {})
        )
        datasets.append(
            TableDataset(
                document_id=catalog.document_id,
                document_version_id=catalog.document_version_id,
                title=title,
                sheet_name=catalog.sheet_name,
                region_index=catalog.region_index,
                columns=columns,
                row_count=catalog.row_count,
                semantic_summary=semantic_summary or "",
                samples=samples,
                literal_matches=(
                    _literal_matches_for_question(
                        question,
                        complete_records,
                        columns,
                    )
                    if complete_records
                    else artifact_literals
                ),
                records=tuple(complete_records),
                dataset_id=catalog.id,
            )
        )
    return datasets


async def _execute_dataset_plan(
    db: AsyncSession,
    dataset: TableDataset,
    plan: TableQueryPlan,
) -> TableQueryResult:
    """Prefer the columnar engine and preserve the proven row fallback."""

    from .dataset_execution import DatasetExecutionError, execute_dataset_query

    if dataset.dataset_id is not None:
        payload = {
            "filters": [
                {"column": item.column, "operator": item.operator, "value": item.value}
                for item in plan.filters
            ],
            "group_by": list(plan.group_by),
            "metric": plan.metric,
            "metric_column": plan.metric_column,
            "sort_by": plan.sort_by,
            "sort_order": plan.sort_order,
            "limit": plan.limit,
        }
        try:
            result = await execute_dataset_query(db, dataset.dataset_id, payload)
            rows = result["rows"]
            warnings = (
                [f"结果共 {result['matched_row_count']} 行，仅返回前 {plan.limit} 行"]
                if result.get("truncated")
                else []
            )
            summary = json.dumps(
                {
                    "数据集": f"{dataset.title} / {dataset.sheet_name} / 区域 {dataset.region_index}",
                    "执行后端": "DuckDB / Parquet",
                    "筛选后行数": result["matched_row_count"],
                    "统计方式": plan.metric,
                    "统计列": plan.metric_column,
                    "分组列": list(plan.group_by),
                    "计算结果": rows,
                    "提示": warnings,
                },
                ensure_ascii=False,
                indent=2,
            )
            return TableQueryResult(
                dataset=dataset,
                plan=plan,
                rows=rows,
                source_rows=result.get("source_rows") or [],
                source_row_start=result.get("source_row_start"),
                source_row_end=result.get("source_row_end"),
                matched_row_count=result["matched_row_count"],
                summary=summary,
                warnings=warnings,
            )
        except DatasetExecutionError as exc:
            if exc.code not in {"artifact_unavailable", "artifact_missing"}:
                raise
    if not dataset.records:
        raise DatasetExecutionError(
            "artifact_unavailable",
            "大型数据集列式产物尚未就绪，无法安全执行不完整查询",
        )
    return execute_query_plan(dataset, dataset.records, plan)


_PLAN_SYSTEM = """你是藏知的表格查询规划器。
只返回 JSON 对象，不生成 SQL、Python 或解释文字。
先根据数据集的 semantic_summary、标题、表名、列名和样例判断问题
是否适合用该表格精确查询。“有什么”“有哪些”等只是弱信号，
不能单独证明相关。若无法把问题映射到表格的字段、筛选、统计或明细，
返回 relevant=false；否则返回 relevant=true 和完整计划。
你只能从给定数据集和列名中选择。计划字段固定为：
relevant、document_id、sheet_name、region_index、filters、group_by、metric、
metric_column、sort_by、sort_order、limit。
filters 每项固定为 column/operator/value，operator 仅可用
eq/ne/gt/gte/lt/lte/contains/starts_with/ends_with/direct_child_of/in。
若字段使用“-”“/”或“>”表达层级路径，问题询问父级实体的可加总指标、但父级本身
可能没有记录，应使用 direct_child_of 只选择直属子级并对数值列求和；不要用
contains 混入更深层后代造成重复统计。普通精确记录仍优先使用 eq。
metric 仅可用 rows/count/count_distinct/sum/avg/min/max。
用户要查看、筛选或排序具体明细时用 rows，group_by=[]。
没有分组但需要总计时 group_by=[]。
排名时设置 group_by、sort_by=metric、sort_order=desc 和 limit。
limit 必须是 1 到 50 之间的整数，不确定时使用 20。
不要臆造列名或数据集。"""


async def try_structured_table_query(
    db: AsyncSession,
    *,
    provider: AIProvider,
    question: str,
    document_ids: Sequence[int],
    force: bool = False,
) -> TableQueryResult | None:
    if not force and not is_structured_table_question(question):
        return None
    datasets = await _load_datasets(db, document_ids, question=question)
    if not datasets:
        return None
    if _DETAIL_INTENT_RE.search(question):
        matched_datasets = [
            dataset
            for dataset in datasets
            if _literal_detail_plan_is_complete(question, dataset)
        ]
        if matched_datasets:
            dataset = max(
                matched_datasets,
                key=lambda item: (
                    len(item.literal_matches),
                    sum(len(value) for _, value in item.literal_matches),
                ),
            )
            plan = TableQueryPlan(
                document_id=dataset.document_id,
                sheet_name=dataset.sheet_name,
                region_index=dataset.region_index,
                filters=tuple(
                    TableFilter(column=column, operator="eq", value=value)
                    for column, value in dataset.literal_matches
                ),
                metric="rows",
                limit=MAX_RESULT_ROWS,
            )
            return await _execute_dataset_plan(db, dataset, plan)
    if _COUNT_INTENT_RE.search(question):
        matched_datasets = [
            dataset
            for dataset in datasets
            if _literal_detail_plan_is_complete(question, dataset)
        ]
        if matched_datasets:
            dataset = max(
                matched_datasets,
                key=lambda item: (
                    len(item.literal_matches),
                    sum(len(value) for _, value in item.literal_matches),
                ),
            )
            plan = TableQueryPlan(
                document_id=dataset.document_id,
                sheet_name=dataset.sheet_name,
                region_index=dataset.region_index,
                filters=tuple(
                    TableFilter(column=column, operator="eq", value=value)
                    for column, value in dataset.literal_matches
                ),
                metric="count",
            )
            return await _execute_dataset_plan(db, dataset, plan)
    prompt = json.dumps(
        {
            "问题": question,
            "可用数据集": [dataset.to_prompt_dict() for dataset in datasets[:12]],
        },
        ensure_ascii=False,
    )
    plan = None
    correction = ""
    for _attempt in range(2):
        try:
            raw_plan = await asyncio.to_thread(
                provider.generate_json,
                system=_PLAN_SYSTEM,
                prompt=prompt + correction,
            )
            if raw_plan.get("relevant") is False:
                return None
            if raw_plan.get("relevant") is not True:
                raise ValueError("查询计划缺少布尔字段 relevant")
            plan = validate_query_plan(raw_plan, datasets)
            break
        except AIProviderError:
            continue
        except (ValueError, TypeError, KeyError) as exc:
            correction = "\n" + json.dumps(
                {
                    "上一次计划": raw_plan,
                    "校验错误": str(exc),
                    "要求": "请按原问题重新输出修正后的完整 JSON 计划。",
                },
                ensure_ascii=False,
            )
    if plan is None:
        return None
    dataset = next(
        item
        for item in datasets
        if item.key == (plan.document_id, plan.sheet_name, plan.region_index)
    )
    result = await _execute_dataset_plan(db, dataset, plan)
    ambiguous_truncation = (
        plan.metric == "rows"
        and result.matched_row_count > len(result.rows)
        and not _DETAIL_INTENT_RE.search(question)
    )
    if result.matched_row_count > 0 and not ambiguous_truncation:
        return result

    feedback = {
        "已执行计划": {
            "filters": [
                {
                    "column": item.column,
                    "operator": item.operator,
                    "value": item.value,
                }
                for item in plan.filters
            ],
            "group_by": list(plan.group_by),
            "metric": plan.metric,
            "metric_column": plan.metric_column,
            "limit": plan.limit,
        },
        "执行结果": {
            "matched_row_count": result.matched_row_count,
            "returned_rows": result.rows[:5],
            "truncated": ambiguous_truncation,
        },
        "修正规则": (
            "不要从零行或任意第一行作答。请重新核对筛选粒度。"
            "若目标是层级路径中的父级、父级自身无记录且指标可加总，"
            "使用 direct_child_of 选择直属子级并 sum 数值列；"
            "若不能安全推导则返回 relevant=false。"
        ),
    }
    try:
        raw_refined = await asyncio.to_thread(
            provider.generate_json,
            system=_PLAN_SYSTEM,
            prompt=prompt + "\n" + json.dumps(feedback, ensure_ascii=False),
        )
        if raw_refined.get("relevant") is not True:
            return result
        refined_plan = validate_query_plan(raw_refined, datasets)
    except (AIProviderError, ValueError, TypeError, KeyError):
        return result
    refined_dataset = next(
        item
        for item in datasets
        if item.key
        == (
            refined_plan.document_id,
            refined_plan.sheet_name,
            refined_plan.region_index,
        )
    )
    return await _execute_dataset_plan(db, refined_dataset, refined_plan)
