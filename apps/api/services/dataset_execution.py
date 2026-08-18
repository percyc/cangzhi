"""Versioned Parquet materialisation and safe DuckDB dataset execution.

The public boundary accepts a constrained query plan, never SQL.  SQL in this
module is generated exclusively from validated field names and operators.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from ..core.config import settings
from ..models.datasets import DatasetArtifact, DatasetField, KnowledgeDataset
from ..models.documents import Document
from ..models.table_rows import StructuredTableRow
from .scope_keys import DocumentSelection, candidate_condition

MAX_PREVIEW_ROWS = 200
MAX_QUERY_RESULT_ROWS = 200
MAX_FILTERS = 8
MAX_IN_VALUES = 100
ALLOWED_OPERATORS = frozenset(
    {
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
)
ALLOWED_METRICS = frozenset(
    {"rows", "count", "count_distinct", "sum", "avg", "min", "max"}
)
_CANONICAL_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T\s].*)?$")


class DatasetExecutionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SafeDatasetQuery:
    filters: tuple[dict[str, Any], ...] = ()
    columns: tuple[str, ...] = ()
    group_by: tuple[str, ...] = ()
    metric: str = "rows"
    metric_column: str | None = None
    sort_by: str | None = None
    sort_order: str = "asc"
    limit: int = 50
    offset: int = 0


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _storage_root(value: str | Path | None = None) -> Path:
    return Path(value or settings.storage_path).resolve()


def _artifact_path(artifact: DatasetArtifact, root: str | Path | None = None) -> Path:
    if not artifact.storage_key:
        raise DatasetExecutionError("artifact_unavailable", "数据集列式产物尚未就绪")
    base = _storage_root(root)
    path = (base / artifact.storage_key).resolve()
    if base != path and base not in path.parents:
        raise DatasetExecutionError("artifact_invalid", "数据集产物路径无效")
    if not path.is_file():
        raise DatasetExecutionError(
            "artifact_missing", "数据集列式产物不存在，请重新构建"
        )
    return path


def _configure_connection(connection: duckdb.DuckDBPyConnection) -> None:
    memory = str(settings.dataset_query_memory_limit).replace("'", "")
    threads = max(1, min(int(settings.dataset_query_threads), 8))
    connection.execute(f"SET memory_limit='{memory}'")
    connection.execute(f"SET threads={threads}")
    connection.execute("SET enable_progress_bar=false")
    connection.execute("SET autoinstall_known_extensions=false")
    connection.execute("SET autoload_known_extensions=false")


def build_dataset_parquet(
    session: Session,
    dataset: KnowledgeDataset,
    *,
    storage_root: str | Path | None = None,
) -> DatasetArtifact:
    """Build a snapshot and atomically make it the dataset's active artifact."""

    fields = list(
        session.scalars(
            select(DatasetField)
            .where(DatasetField.dataset_id == dataset.id)
            .order_by(DatasetField.position)
        ).all()
    )
    row_statement = (
        select(StructuredTableRow)
        .where(StructuredTableRow.dataset_id == dataset.id)
        .order_by(StructuredTableRow.row_number)
    )
    first_row = session.scalar(row_statement.limit(1))
    if not fields and first_row is not None:
        fields = [
            DatasetField(dataset_id=dataset.id, position=index, name=name)
            for index, name in enumerate((first_row.values or {}).keys())
        ]
    if not fields:
        raise DatasetExecutionError("dataset_empty", "数据集没有可执行字段")

    next_version = (
        int(
            session.scalar(
                select(
                    func.coalesce(func.max(DatasetArtifact.version_number), 0)
                ).where(DatasetArtifact.dataset_id == dataset.id)
            )
            or 0
        )
        + 1
    )
    row_count = int(
        session.scalar(
            select(func.count(StructuredTableRow.id)).where(
                StructuredTableRow.dataset_id == dataset.id
            )
        )
        or 0
    )
    artifact = DatasetArtifact(
        dataset_id=dataset.id,
        version_number=next_version,
        format="parquet",
        status="building",
        row_count=row_count,
        schema_snapshot={
            "fields": [
                {
                    "name": field.name,
                    "type": field.inferred_type,
                    "position": field.position,
                }
                for field in fields
            ]
        },
    )
    session.add(artifact)
    session.flush()

    root = _storage_root(storage_root)
    relative = Path("datasets") / str(dataset.id) / f"v{next_version}.parquet"
    final_path = root / relative
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = final_path.with_suffix(f".parquet.tmp-{os.getpid()}-{artifact.id}")
    temporary_json = final_path.with_suffix(f".ndjson.tmp-{os.getpid()}-{artifact.id}")
    columns = [field.name for field in fields]
    connection = duckdb.connect(database=":memory:")
    try:
        with temporary_json.open("w", encoding="utf-8") as output:
            for row in session.scalars(
                row_statement.execution_options(yield_per=2_000)
            ):
                record = {
                    "__row_number": row.row_number,
                    **{
                        column: (
                            None
                            if (row.values or {}).get(column) is None
                            else str((row.values or {}).get(column))
                        )
                        for column in columns
                    },
                }
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        _configure_connection(connection)
        projection = ["CAST(__row_number AS BIGINT) AS __row_number"] + [
            f"CAST({_quote_identifier(column)} AS VARCHAR) AS {_quote_identifier(column)}"
            for column in columns
        ]
        if row_count:
            connection.execute(
                f"CREATE TABLE dataset AS SELECT {', '.join(projection)} "
                "FROM read_json_auto(?, format='newline_delimited', "
                "maximum_object_size=16777216)",
                [str(temporary_json)],
            )
        else:
            definitions = ["__row_number BIGINT"] + [
                f"{_quote_identifier(column)} VARCHAR" for column in columns
            ]
            connection.execute(f"CREATE TABLE dataset ({', '.join(definitions)})")
        connection.execute(
            "COPY dataset TO ? (FORMAT PARQUET, COMPRESSION ZSTD)", [str(temporary)]
        )
    except Exception as exc:
        artifact.status = "failed"
        artifact.error_message = str(exc)[:2000]
        session.add(artifact)
        if temporary.exists():
            temporary.unlink()
        raise DatasetExecutionError(
            "artifact_build_failed", f"列式产物构建失败：{exc}"
        ) from exc
    finally:
        connection.close()
        if temporary_json.exists():
            temporary_json.unlink()

    checksum = hashlib.sha256(temporary.read_bytes()).hexdigest()
    session.execute(
        update(DatasetArtifact)
        .where(DatasetArtifact.dataset_id == dataset.id)
        .values(is_active=False)
    )
    artifact.status = "ready"
    artifact.storage_key = relative.as_posix()
    artifact.checksum = checksum
    artifact.byte_size = temporary.stat().st_size
    artifact.is_active = True
    artifact.built_at = dt.datetime.now(dt.timezone.utc)
    artifact.error_message = None
    profile = dict(dataset.profile or {})
    profile["execution"] = {
        "backend": "duckdb",
        "format": "parquet",
        "artifact_version": next_version,
        "status": "ready",
    }
    dataset.profile = profile
    session.add_all([artifact, dataset])
    # Surface constraint/DB errors before publishing the file. The caller
    # still owns the transaction and removes already-published sibling files
    # if the final commit fails.
    session.flush()
    try:
        os.replace(temporary, final_path)
    except OSError as exc:
        if temporary.exists():
            temporary.unlink()
        raise DatasetExecutionError(
            "artifact_build_failed", f"列式产物保存失败：{exc}"
        ) from exc
    return artifact


async def get_visible_dataset(db: AsyncSession, dataset_id: int) -> KnowledgeDataset:
    dataset = await db.scalar(
        select(KnowledgeDataset)
        .join(Document, Document.id == KnowledgeDataset.document_id)
        .where(
            KnowledgeDataset.id == dataset_id,
            Document.is_deleted.is_(False),
            Document.current_version_id == KnowledgeDataset.document_version_id,
        )
    )
    if dataset is None:
        raise DatasetExecutionError("dataset_not_found", "数据集不存在")
    return dataset


async def list_visible_datasets(
    db: AsyncSession,
    *,
    document_id: int | None = None,
    limit: int = 100,
    document_selection: DocumentSelection | None = None,
) -> list[dict[str, Any]]:
    statement = (
        select(KnowledgeDataset)
        .join(Document, Document.id == KnowledgeDataset.document_id)
        .where(
            Document.is_deleted.is_(False),
            Document.current_version_id == KnowledgeDataset.document_version_id,
        )
        .order_by(KnowledgeDataset.id.desc())
        .limit(max(1, min(limit, 200)))
    )
    if document_id is not None:
        statement = statement.where(KnowledgeDataset.document_id == document_id)
    if document_selection is not None:
        condition = candidate_condition(document_selection)
        if condition is not None:
            statement = statement.where(condition)
    items = list((await db.scalars(statement)).all())
    return [
        {
            "id": item.id,
            "document_id": item.document_id,
            "name": item.name,
            "sheet_name": item.sheet_name,
            "region_index": item.region_index,
            "row_count": item.row_count,
            "column_count": item.column_count,
            "status": item.status,
        }
        for item in items
    ]


async def get_dataset_schema(db: AsyncSession, dataset_id: int) -> dict[str, Any]:
    dataset = await get_visible_dataset(db, dataset_id)
    fields = list(
        (
            await db.scalars(
                select(DatasetField)
                .where(DatasetField.dataset_id == dataset.id)
                .order_by(DatasetField.position)
            )
        ).all()
    )
    artifact = await db.scalar(
        select(DatasetArtifact).where(
            DatasetArtifact.dataset_id == dataset.id,
            DatasetArtifact.is_active.is_(True),
            DatasetArtifact.status == "ready",
        )
    )
    return {
        "id": dataset.id,
        "document_id": dataset.document_id,
        "document_version_id": dataset.document_version_id,
        "name": dataset.name,
        "sheet_name": dataset.sheet_name,
        "region_index": dataset.region_index,
        "row_count": dataset.row_count,
        "column_count": dataset.column_count,
        "profile": dataset.profile or {},
        "fields": [
            {
                "name": field.name,
                "position": field.position,
                "inferred_type": field.inferred_type,
                "semantic_role": field.semantic_role,
                "null_count": field.null_count,
                "distinct_count": field.distinct_count,
                "sample_values": field.sample_values or [],
                "statistics": field.statistics or {},
            }
            for field in fields
        ],
        "execution": {
            "backend": "duckdb" if artifact else "postgresql_fallback",
            "artifact_status": artifact.status if artifact else "unavailable",
            "artifact_version": artifact.version_number if artifact else None,
            "format": artifact.format if artifact else None,
        },
    }


def _date_aliases(value: str) -> tuple[str, ...]:
    match = _CANONICAL_DATE_RE.match(value.strip())
    if match is None:
        return ()
    year, month, day = (int(item) for item in match.groups())
    return (
        f"{year}年{month}月{day}日",
        f"{year}-{month:02d}-{day:02d}",
        f"{month}月{day}日",
        f"{month:02d}月{day:02d}日",
        f"{month}-{day}",
        f"{month:02d}-{day:02d}",
        f"{month}/{day}",
        f"{month:02d}/{day:02d}",
    )


def _discover_literals(
    path: Path, question: str, fields: Sequence[DatasetField]
) -> tuple[tuple[str, str], ...]:
    connection = duckdb.connect(database=":memory:")
    timeout = threading.Timer(
        float(settings.dataset_query_timeout_seconds), connection.interrupt
    )
    timeout.daemon = True
    matches: list[tuple[str, str]] = []
    try:
        _configure_connection(connection)
        timeout.start()
        for field in fields[:32]:
            quoted = _quote_identifier(field.name)
            if field.semantic_role == "time" or field.inferred_type == "date":
                candidates = [
                    str(item[0])
                    for item in connection.execute(
                        f"SELECT DISTINCT {quoted} FROM read_parquet(?) "
                        f"WHERE {quoted} IS NOT NULL LIMIT 5000",
                        [str(path)],
                    ).fetchall()
                ]
                date_hits = [
                    value
                    for value in candidates
                    if any(
                        re.search(rf"(?<!\d){re.escape(alias)}(?!\d)", question)
                        for alias in _date_aliases(value)
                    )
                ]
                if len(set(date_hits)) == 1:
                    matches.append((field.name, date_hits[0]))
                    continue
            if field.inferred_type in {
                "number",
                "identifier",
            } and field.semantic_role not in {"status", "category"}:
                # A bare year or amount frequently appears somewhere in a
                # numeric/id column by coincidence. Do not treat that as an
                # explicit field literal unless a semantic field handled it.
                continue
            candidates = [
                str(item[0])
                for item in connection.execute(
                    f"SELECT DISTINCT {quoted} FROM read_parquet(?) "
                    f"WHERE {quoted} IS NOT NULL AND length({quoted}) BETWEEN 2 AND 80 "
                    f"AND strpos(lower(?), lower({quoted})) > 0 LIMIT 20",
                    [str(path), question],
                ).fetchall()
            ]
            if candidates:
                matches.append((field.name, max(candidates, key=len)))
                continue
            if (
                field.semantic_role in {"status", "category"}
                or field.inferred_type == "boolean"
            ):
                bound_values = connection.execute(
                    f"SELECT DISTINCT {quoted} FROM read_parquet(?) "
                    f"WHERE {quoted} IS NOT NULL LIMIT 500",
                    [str(path)],
                ).fetchall()
                explicit = [
                    str(item[0])
                    for item in bound_values
                    if re.search(
                        rf"{re.escape(field.name)}\s*(?:为|是|等于|属于|：|:|=)\s*"
                        rf"{re.escape(str(item[0]))}(?:\s|的|、|，|。|？|\?|$)",
                        question,
                        re.IGNORECASE,
                    )
                ]
                if explicit:
                    matches.append((field.name, max(explicit, key=len)))
        return tuple(matches)
    finally:
        timeout.cancel()
        connection.close()


async def discover_dataset_literals(
    db: AsyncSession,
    dataset_id: int,
    question: str,
    *,
    storage_root: str | Path | None = None,
) -> tuple[tuple[str, str], ...]:
    artifact = await db.scalar(
        select(DatasetArtifact).where(
            DatasetArtifact.dataset_id == dataset_id,
            DatasetArtifact.status == "ready",
            DatasetArtifact.is_active.is_(True),
        )
    )
    if artifact is None:
        return ()
    fields = list(
        (
            await db.scalars(
                select(DatasetField)
                .where(DatasetField.dataset_id == dataset_id)
                .order_by(DatasetField.position)
            )
        ).all()
    )
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _discover_literals,
                _artifact_path(artifact, storage_root),
                question,
                fields,
            ),
            timeout=float(settings.dataset_query_timeout_seconds) + 1,
        )
    except (DatasetExecutionError, TimeoutError, duckdb.Error):
        return ()


def validate_safe_query(
    raw: dict[str, Any], field_names: Sequence[str]
) -> SafeDatasetQuery:
    allowed_keys = {
        "filters",
        "columns",
        "group_by",
        "metric",
        "metric_column",
        "sort_by",
        "sort_order",
        "limit",
        "offset",
    }
    if not isinstance(raw, dict) or set(raw) - allowed_keys:
        raise DatasetExecutionError("invalid_query", "查询计划包含不允许的字段")
    available = set(field_names)
    filters = raw.get("filters") or []
    if not isinstance(filters, list) or len(filters) > MAX_FILTERS:
        raise DatasetExecutionError("invalid_query", "筛选条件数量无效")
    clean_filters: list[dict[str, Any]] = []
    for item in filters:
        if not isinstance(item, dict):
            raise DatasetExecutionError("invalid_query", "筛选条件格式无效")
        keys = frozenset(item)
        canonical_keys = {"column", "operator", "value"}
        alias_keys = {"column", "op", "value"}
        if keys not in {frozenset(canonical_keys), frozenset(alias_keys)}:
            raise DatasetExecutionError("invalid_query", "筛选条件格式无效")
        column = str(item["column"])
        operator = str(item.get("operator", item.get("op")))
        value = item["value"]
        if column not in available or operator not in ALLOWED_OPERATORS:
            raise DatasetExecutionError("invalid_query", "筛选列或运算符无效")
        if operator == "in" and (
            not isinstance(value, list) or len(value) > MAX_IN_VALUES
        ):
            raise DatasetExecutionError("invalid_query", "in 条件最多支持 100 个值")
        if operator != "in" and isinstance(value, (dict, list)):
            raise DatasetExecutionError("invalid_query", "筛选值必须是标量")
        if value is None and operator not in {"eq", "ne"}:
            raise DatasetExecutionError("invalid_query", "只有 eq/ne 可以使用空值筛选")
        clean_filters.append({"column": column, "operator": operator, "value": value})
    columns = tuple(str(item) for item in (raw.get("columns") or []))
    group_by = tuple(str(item) for item in (raw.get("group_by") or []))
    if any(item not in available for item in columns + group_by) or len(group_by) > 3:
        raise DatasetExecutionError("invalid_query", "投影或分组字段无效")
    metric = str(raw.get("metric") or "rows")
    metric_column = raw.get("metric_column")
    metric_column = str(metric_column) if metric_column else None
    if metric not in ALLOWED_METRICS or (
        metric not in {"rows", "count"} and metric_column not in available
    ):
        raise DatasetExecutionError("invalid_query", "统计方式或统计字段无效")
    if metric == "rows" and group_by:
        raise DatasetExecutionError("invalid_query", "明细查询不能同时分组")
    sort_by = str(raw["sort_by"]) if raw.get("sort_by") else None
    permitted_sort = (
        available | {"row_number"}
        if metric == "rows"
        else set(group_by) | {"metric", "matched_rows"}
    )
    if sort_by and sort_by not in permitted_sort:
        raise DatasetExecutionError("invalid_query", "排序字段无效")
    sort_order = str(raw.get("sort_order") or "asc").lower()
    if sort_order not in {"asc", "desc"}:
        raise DatasetExecutionError("invalid_query", "排序方向无效")
    limit = int(raw.get("limit") or 50)
    offset = int(raw.get("offset") or 0)
    if limit < 1 or limit > MAX_QUERY_RESULT_ROWS or offset < 0 or offset > 1_000_000:
        raise DatasetExecutionError("invalid_query", "分页范围无效")
    return SafeDatasetQuery(
        tuple(clean_filters),
        columns,
        group_by,
        metric,
        metric_column,
        sort_by,
        sort_order,
        limit,
        offset,
    )


def _typed_expression(column: str, inferred_type: str) -> str:
    quoted = _quote_identifier(column)
    if inferred_type == "number":
        return (
            "CASE WHEN regexp_matches("
            f"{quoted}, '（原始值：[^）]+）$') THEN "
            f"TRY_CAST(regexp_extract({quoted}, '（原始值：([^）]+)）$', 1) AS DOUBLE) "
            "ELSE "
            f"TRY_CAST(regexp_replace({quoted}, '[,，[:space:]￥¥$元%]', '', 'g') AS DOUBLE) "
            f"/ CASE WHEN right(trim({quoted}), 1) = '%' THEN 100 ELSE 1 END END"
        )
    if inferred_type == "date":
        return f"TRY_CAST({quoted} AS DATE)"
    return quoted


def _where_sql(query: SafeDatasetQuery, types: dict[str, str]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    parameters: list[Any] = []
    operators = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
    for item in query.filters:
        expression = _typed_expression(
            item["column"], types.get(item["column"], "text")
        )
        operator, value = item["operator"], item["value"]
        if operator == "contains":
            clauses.append(f"COALESCE({expression}, '') ILIKE ?")
            parameters.append(f"%{value}%")
        elif operator == "starts_with":
            clauses.append(f"COALESCE({expression}, '') ILIKE ?")
            parameters.append(f"{value}%")
        elif operator == "ends_with":
            clauses.append(f"COALESCE({expression}, '') ILIKE ?")
            parameters.append(f"%{value}")
        elif operator == "direct_child_of":
            parent = re.escape(str(value).strip())
            clauses.append(f"regexp_matches(COALESCE({expression}, ''), ?)")
            parameters.append(rf"(^|[-/＞>]){parent}[-/＞>][^-/＞>]+$")
        elif operator == "in":
            values = value or []
            if not values:
                clauses.append("FALSE")
            else:
                clauses.append(f"{expression} IN ({', '.join('?' for _ in values)})")
                parameters.extend(values)
        elif value is None and operator in {"eq", "ne"}:
            clauses.append(f"{expression} IS {'NOT ' if operator == 'ne' else ''}NULL")
        else:
            clauses.append(f"{expression} {operators[operator]} ?")
            parameters.append(value)
    return (" AND ".join(clauses) if clauses else "TRUE"), parameters


def _execute_parquet_query(
    path: Path, query: SafeDatasetQuery, fields: Sequence[DatasetField]
) -> dict[str, Any]:
    names = [field.name for field in fields]
    types = {field.name: field.inferred_type for field in fields}
    where, parameters = _where_sql(query, types)
    connection = duckdb.connect(database=":memory:")
    timeout = threading.Timer(
        float(settings.dataset_query_timeout_seconds), connection.interrupt
    )
    timeout.daemon = True
    try:
        _configure_connection(connection)
        timeout.start()
        source = "read_parquet(?)"
        base_parameters = [str(path), *parameters]
        matched_row = connection.execute(
            f"SELECT COUNT(*), MIN(__row_number), MAX(__row_number) FROM {source} WHERE {where}",
            base_parameters,
        ).fetchone()
        matched = int(matched_row[0] or 0)
        reference_rows = [
            int(item[0])
            for item in connection.execute(
                f"SELECT __row_number FROM {source} WHERE {where} "
                "ORDER BY __row_number LIMIT 200",
                base_parameters,
            ).fetchall()
        ]
        if query.metric == "rows":
            selected = list(query.columns) if query.columns else names
            projection = ["__row_number AS row_number"] + [
                _quote_identifier(name) for name in selected
            ]
            order = query.sort_by or "__row_number"
            order_sql = (
                _quote_identifier(order) if order != "row_number" else "__row_number"
            )
            sql = (
                f"SELECT {', '.join(projection)} FROM {source} WHERE {where} "
                f"ORDER BY {order_sql} {query.sort_order.upper()} LIMIT ? OFFSET ?"
            )
            cursor = connection.execute(
                sql, [str(path), *parameters, query.limit, query.offset]
            )
        else:
            groups = [_quote_identifier(name) for name in query.group_by]
            if query.metric == "count":
                metric_sql = "COUNT(*)"
            elif query.metric == "count_distinct":
                metric_sql = (
                    f"COUNT(DISTINCT {_quote_identifier(query.metric_column or '')})"
                )
            else:
                expression = _typed_expression(
                    query.metric_column or "",
                    types.get(query.metric_column or "", "text"),
                )
                metric_sql = f"{query.metric.upper()}({expression})"
            projection = groups + [
                f"{metric_sql} AS metric",
                "COUNT(*) AS matched_rows",
            ]
            group_sql = f" GROUP BY {', '.join(groups)}" if groups else ""
            order = query.sort_by or "metric"
            order_sql = _quote_identifier(order) if order in query.group_by else order
            sql = (
                f"SELECT {', '.join(projection)} FROM {source} WHERE {where}{group_sql} "
                f"ORDER BY {order_sql} {query.sort_order.upper()} LIMIT ? OFFSET ?"
            )
            cursor = connection.execute(
                sql, [str(path), *parameters, query.limit, query.offset]
            )
        columns = [item[0] for item in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        source_rows = [
            int(row["row_number"]) for row in rows if "row_number" in row
        ] or reference_rows
        return {
            "backend": "duckdb",
            "rows": rows,
            "matched_row_count": matched,
            "source_rows": source_rows,
            "source_row_start": int(matched_row[1])
            if matched_row[1] is not None
            else None,
            "source_row_end": int(matched_row[2])
            if matched_row[2] is not None
            else None,
            "truncated": matched > query.offset + len(rows)
            if query.metric == "rows"
            else False,
        }
    finally:
        timeout.cancel()
        connection.close()


async def execute_dataset_query(
    db: AsyncSession,
    dataset_id: int,
    raw_query: dict[str, Any],
    *,
    storage_root: str | Path | None = None,
) -> dict[str, Any]:
    dataset = await get_visible_dataset(db, dataset_id)
    fields = list(
        (
            await db.scalars(
                select(DatasetField)
                .where(DatasetField.dataset_id == dataset.id)
                .order_by(DatasetField.position)
            )
        ).all()
    )
    query = validate_safe_query(raw_query, [field.name for field in fields])
    artifact = await db.scalar(
        select(DatasetArtifact).where(
            DatasetArtifact.dataset_id == dataset.id,
            DatasetArtifact.status == "ready",
            DatasetArtifact.is_active.is_(True),
        )
    )
    if artifact is None:
        # Existing installations remain queryable while migration 0022 builds
        # their snapshots. Never calculate over a truncated row set.
        from .structured_table import (
            MAX_QUERY_ROWS,
            TableDataset,
            TableFilter,
            TableQueryPlan,
            execute_query_plan,
        )

        records = list(
            (
                await db.scalars(
                    select(StructuredTableRow)
                    .where(StructuredTableRow.dataset_id == dataset.id)
                    .order_by(StructuredTableRow.row_number)
                    .limit(MAX_QUERY_ROWS + 1)
                )
            ).all()
        )
        if len(records) > MAX_QUERY_ROWS:
            raise DatasetExecutionError(
                "artifact_unavailable",
                "大型数据集列式产物尚未构建，不能返回可能不完整的结果",
            )
        document = await db.get(Document, dataset.document_id)
        fallback_dataset = TableDataset(
            document_id=dataset.document_id,
            document_version_id=dataset.document_version_id,
            title=document.title if document else dataset.name,
            sheet_name=dataset.sheet_name,
            region_index=dataset.region_index,
            columns=tuple(field.name for field in fields),
            row_count=len(records),
            records=tuple(records),
            dataset_id=dataset.id,
        )
        fallback_plan = TableQueryPlan(
            document_id=dataset.document_id,
            sheet_name=dataset.sheet_name,
            region_index=dataset.region_index,
            filters=tuple(
                TableFilter(item["column"], item["operator"], item["value"])
                for item in query.filters
            ),
            group_by=query.group_by,
            metric=query.metric,
            metric_column=query.metric_column,
            sort_by=query.sort_by,
            sort_order=query.sort_order,
            limit=min(query.limit + query.offset, 50),
        )
        fallback = execute_query_plan(fallback_dataset, records, fallback_plan)
        rows = fallback.rows[query.offset : query.offset + query.limit]
        if query.metric == "rows" and query.columns:
            rows = [
                {
                    key: value
                    for key, value in row.items()
                    if key == "row_number" or key in query.columns
                }
                for row in rows
            ]
        payload = {
            "dataset_id": dataset.id,
            "document_id": dataset.document_id,
            "artifact_version": None,
            "backend": "postgresql_fallback",
            "rows": rows,
            "matched_row_count": fallback.matched_row_count,
            "source_rows": fallback.source_rows,
            "source_row_start": fallback.source_row_start,
            "source_row_end": fallback.source_row_end,
            "truncated": fallback.matched_row_count > query.offset + len(rows),
            "warnings": (
                ["列式产物尚未就绪，已使用兼容执行器并将单页结果限制为 50 行"]
                if query.limit + query.offset > 50
                else ["列式产物尚未就绪，当前使用 PostgreSQL 兼容执行器"]
            ),
        }
        payload["query_hints"] = _query_hints(query, fields, payload)
        return payload
    path = _artifact_path(artifact, storage_root)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_execute_parquet_query, path, query, fields),
            timeout=float(settings.dataset_query_timeout_seconds),
        )
    except TimeoutError as exc:
        raise DatasetExecutionError(
            "query_timeout", "数据集查询超时，请缩小筛选范围"
        ) from exc
    except duckdb.Error as exc:
        if "interrupt" in str(exc).casefold():
            raise DatasetExecutionError(
                "query_timeout", "数据集查询超时，请缩小筛选范围"
            ) from exc
        raise DatasetExecutionError("query_failed", f"数据集查询失败：{exc}") from exc
    payload = {
        "dataset_id": dataset.id,
        "document_id": dataset.document_id,
        "artifact_version": artifact.version_number,
        "warnings": [],
        **result,
    }
    payload["query_hints"] = _query_hints(query, fields, payload)
    return payload


def _query_hints(
    query: SafeDatasetQuery,
    fields: list[DatasetField],
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return machine-readable recovery hints without changing query meaning."""

    if int(result.get("matched_row_count") or 0) > 0:
        return []
    field_map = {field.name: field for field in fields}
    hints: list[dict[str, Any]] = []
    for condition in query.filters:
        if condition.get("operator") != "eq":
            continue
        value = condition.get("value")
        if not isinstance(value, str) or not value.strip():
            continue
        field = field_map.get(str(condition.get("column") or ""))
        if field is None or field.inferred_type not in {"text", "identifier"}:
            continue
        if not any(
            _sample_contains_descendant(sample, value)
            for sample in list(field.sample_values or [])
        ):
            continue
        hints.append(
            {
                "type": "hierarchy_children_available",
                "message": (
                    "精确父级没有记录，但字段样例显示存在下级路径；"
                    "可保留其他筛选条件，将该条件改为 direct_child_of 验证直属子级。"
                ),
                "suggested_filter": {
                    "column": field.name,
                    "operator": "direct_child_of",
                    "value": value,
                },
            }
        )
    return hints


def _sample_contains_descendant(sample: Any, parent: str) -> bool:
    text = str(sample or "").strip().casefold()
    target = parent.strip().casefold()
    if not text or not target:
        return False
    pattern = rf"(^|[-/＞>]){re.escape(target)}[-/＞>]"
    return re.search(pattern, text) is not None


async def preview_dataset(
    db: AsyncSession,
    dataset_id: int,
    *,
    offset: int = 0,
    limit: int = 50,
    storage_root: str | Path | None = None,
) -> dict[str, Any]:
    return await execute_dataset_query(
        db,
        dataset_id,
        {"metric": "rows", "limit": min(limit, MAX_PREVIEW_ROWS), "offset": offset},
        storage_root=storage_root,
    )
