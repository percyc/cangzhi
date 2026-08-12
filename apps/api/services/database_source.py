"""Read-only connectors to external PostgreSQL / MySQL instances.

This module owns the safety boundary for connecting to a remote database:

* the host is validated: loopback / link-local / unspecified / multicast are
  always rejected; private (non-global) addresses only when
  ``trusted_private_network`` is set;
* the SQLAlchemy URL is built from validated components, never from a free-form
  DSN, so credentials and identifiers cannot be injected;
* every connection runs in a read-only transaction with a statement timeout;
* catalog and import SQL is generated only from identifiers that were returned
  by the catalog and validated against a safe pattern, with values passed as
  bound parameters.

The real driver work is synchronous and runs inside ``asyncio.to_thread`` so the
API event loop is never blocked. Tests replace the ``_*_impl`` driver functions
with fakes and never need a real database server.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import URL, Connection, Engine, create_engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from ..core.config import settings as core_settings
from ..models.database_source import DatabaseSnapshot, DatabaseSource
from ..models.datasets import DatasetArtifact, DatasetField, KnowledgeDataset
from ..models.documents import Document, DocumentSourceType, DocumentVersion
from ..models.table_rows import StructuredTableRow
from ..security.secrets import decrypt_secret
from .dataset_execution import DatasetExecutionError, build_dataset_parquet

CONNECT_TIMEOUT_SECONDS = 10
STATEMENT_TIMEOUT_MS = 30_000
DEFAULT_ENGINE_PORT = {"postgresql": 5432, "mysql": 3306}
ENGINE_DRIVER = {
    "postgresql": "postgresql+psycopg2",
    "mysql": "mysql+pymysql",
}
# First version caps: bounded snapshots by design, not by accident.
MAX_IMPORT_ROWS = 100_000
MAX_CELL_SIZE = 4_096
MAX_CATALOG_TABLES = 500
MAX_SCHEMAS = 200
MAX_COLUMNS = 1_000

BIT_DATA_TYPES = {"bit"}

_ENGINE_VALUES_PUBLIC = ("postgresql", "mysql")
_SSL_MODES = {
    "postgresql": {"disable", "prefer", "require"},
    "mysql": {"disabled", "required"},
}


class DatabaseSourceError(RuntimeError):
    """Domain error carrying a stable machine-readable ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DatabaseColumn:
    name: str
    data_type: str
    nullable: bool
    is_primary_key: bool


@dataclass(frozen=True)
class DatabaseTable:
    schema_name: str
    table_name: str
    kind: str
    columns: tuple[DatabaseColumn, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------
# Host / identifier validation (pure, unit-testable)
# --------------------------------------------------------------------------


def _ip_must_always_be_rejected(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return _ip_must_always_be_rejected(ip.ipv4_mapped)
    return ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast


def _validate_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    trusted_private_network: bool,
) -> None:
    if _ip_must_always_be_rejected(ip):
        raise DatabaseSourceError(
            "host_forbidden",
            "数据库地址指向本机、链路本地、未指定或多播地址，已拒绝连接",
        )
    if not ip.is_global and not trusted_private_network:
        raise DatabaseSourceError(
            "host_private",
            "数据库地址解析为内网地址；如需连接可信内网，请显式选择“允许可信内网”",
        )


def _validate_ip_literal(
    host: str,
    *,
    trusted_private_network: bool,
) -> bool:
    candidate = host.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    _validate_ip(ip, trusted_private_network=trusted_private_network)
    return True


def validate_database_host(
    host: str,
    *,
    port: int | None = None,
    trusted_private_network: bool = False,
    resolve: bool = True,
) -> str:
    """Return a normalized hostname after enforcing the network boundary.

    Literal loopback / link-local / unspecified / multicast addresses are always
    refused. Private (non-global) addresses are refused unless
    ``trusted_private_network`` is set. Hostnames are resolved (unless
    ``resolve=False`` for a pure format check) and every result validated.
    """
    raw = (host or "").strip()
    if not raw:
        raise DatabaseSourceError("host_missing", "数据库主机不能为空")
    if any(ord(char) < 0x20 for char in raw):
        raise DatabaseSourceError("host_invalid", "数据库主机名包含非法字符")
    candidate = raw
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    normalized = candidate.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        raise DatabaseSourceError("host_loopback", "数据库连接不能指向本机")

    if _validate_ip_literal(candidate, trusted_private_network=trusted_private_network):
        return candidate

    if not resolve:
        return raw

    try:
        infos = socket.getaddrinfo(candidate, port or 1)
    except socket.gaierror as exc:
        raise DatabaseSourceError(
            "host_unresolved", f"无法解析数据库主机：{exc}"
        ) from exc
    seen: set[tuple[str, str]] = set()
    for family, _, _, _, sockaddr in infos:
        address = str(sockaddr[0]).split("%", 1)[0]
        key = (str(family), address)
        if key in seen:
            continue
        seen.add(key)
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise DatabaseSourceError(
                "host_invalid", "数据库主机解析结果无效"
            ) from None
        _validate_ip(ip, trusted_private_network=trusted_private_network)
    return raw


def validate_identifier(value: str, kind: str) -> str:
    """Validate a catalog identifier without imposing ASCII-only rules.

    PostgreSQL and MySQL both support quoted Unicode identifiers (including
    spaces). Safety comes from exact catalog membership plus driver-specific
    quoting, not from pretending every valid database uses ``[A-Za-z0-9_]``.
    """

    cleaned = (value or "").strip()
    if not cleaned:
        raise DatabaseSourceError("invalid_identifier", f"{kind} 不能为空")
    if len(cleaned) > 255:
        raise DatabaseSourceError("invalid_identifier", f"{kind} 标识符过长")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in cleaned):
        raise DatabaseSourceError(
            "invalid_identifier",
            f"{kind} 包含控制字符",
        )
    return cleaned


def validate_ssl_mode(engine: str, value: str) -> str:
    modes = _SSL_MODES.get(engine)
    if modes is None:
        raise DatabaseSourceError("unsupported_engine", f"不支持的数据库类型：{engine}")
    cleaned = (value or "").strip().lower()
    if cleaned not in modes:
        raise DatabaseSourceError(
            "invalid_ssl_mode",
            f"{engine} 的 SSL/TLS 模式必须是：{', '.join(sorted(modes))}",
        )
    return cleaned


def _quote_driver_identifier(engine: str, value: str) -> str:
    if engine == "mysql":
        return "`" + value.replace("`", "``") + "`"
    return '"' + value.replace('"', '""') + '"'


# --------------------------------------------------------------------------
# Real driver layer (synchronous; never called on the event loop directly)
# --------------------------------------------------------------------------


def _build_url(source: DatabaseSource, host: str) -> URL:
    driver = ENGINE_DRIVER.get(source.engine)
    if driver is None:
        raise DatabaseSourceError(
            "unsupported_engine", f"不支持的数据库类型：{source.engine}"
        )
    password = decrypt_secret(source.password_cipher)
    return URL.create(
        driver,
        username=source.username or None,
        password=password or None,
        host=host,
        port=source.port,
        database=source.database_name,
    )


@contextmanager
def _safe_connect(source: DatabaseSource) -> Iterator[tuple[Engine, Connection]]:
    host = validate_database_host(
        source.host,
        port=source.port,
        trusted_private_network=source.trusted_private_network,
    )
    url = _build_url(source, host=host)
    ssl_mode = validate_ssl_mode(source.engine, source.ssl_mode)
    if source.engine == "postgresql":
        connect_args: dict[str, Any] = {
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            "sslmode": ssl_mode,
        }
    else:
        connect_args = {
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            "read_timeout": CONNECT_TIMEOUT_SECONDS,
            "write_timeout": CONNECT_TIMEOUT_SECONDS,
        }
        if ssl_mode == "required":
            # A non-empty mapping enables TLS in PyMySQL. Certificate/identity
            # verification needs a CA bundle, which is intentionally deferred
            # rather than presenting unsafe controls that do nothing.
            connect_args["ssl"] = {"check_hostname": False}
        else:
            connect_args["ssl_disabled"] = True
    engine = create_engine(url, poolclass=NullPool, connect_args=connect_args)
    connection = engine.connect()
    try:
        _apply_readonly(connection, source.engine)
        yield engine, connection
    finally:
        connection.close()
        engine.dispose()


def _apply_readonly(connection: Connection, engine: str) -> None:
    if engine == "postgresql":
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        connection.exec_driver_sql(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
    elif engine == "mysql":
        connection.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
        connection.exec_driver_sql(
            f"SET SESSION MAX_EXECUTION_TIME = {STATEMENT_TIMEOUT_MS}"
        )
    else:
        raise DatabaseSourceError("unsupported_engine", f"不支持的数据库类型：{engine}")


def _list_schemas_impl(source: DatabaseSource) -> list[str]:
    with _safe_connect(source) as (_engine, connection):
        return _list_schemas_impl_from_connection(connection, source.engine)


def _list_schemas_impl_from_connection(
    connection: Connection, engine: str
) -> list[str]:
    if engine == "postgresql":
        statement = (
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('pg_catalog', 'information_schema') "
            "AND schema_name NOT LIKE 'pg\\\\_%' "
            "ORDER BY schema_name"
        )
    else:
        statement = (
            "SELECT SCHEMA_NAME FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('information_schema', 'mysql', "
            "'performance_schema', 'sys') ORDER BY schema_name"
        )
    rows = connection.exec_driver_sql(statement).fetchall()
    names = [str(row[0]) for row in rows if row[0] is not None]
    return names[:MAX_SCHEMAS]


def _list_tables_impl(
    connection: Connection, engine: str, schema: str
) -> list[tuple[str, str]]:
    statement = (
        "SELECT table_name, table_type FROM information_schema.tables "
        "WHERE table_schema = %s AND table_type IN ('BASE TABLE', 'VIEW') "
        "ORDER BY table_name"
    )
    result = connection.exec_driver_sql(statement, (schema,))
    return [(str(row[0]), str(row[1])) for row in result][:MAX_CATALOG_TABLES]


def _fetch_primary_keys_impl(
    connection: Connection, engine: str, schema: str, table: str
) -> set[str]:
    statement = (
        "SELECT kcu.column_name "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "ON tc.constraint_name = kcu.constraint_name "
        "AND tc.constraint_schema = kcu.constraint_schema "
        "WHERE tc.constraint_type = 'PRIMARY KEY' "
        "AND tc.table_schema = %s AND tc.table_name = %s"
    )
    result = connection.exec_driver_sql(statement, (schema, table))
    return {str(row[0]) for row in result if row[0] is not None}


def _fetch_columns_impl(
    connection: Connection, engine: str, schema: str, table: str
) -> list[tuple[str, str, bool]]:
    statement = (
        "SELECT column_name, data_type, is_nullable, ordinal_position "
        "FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s "
        "ORDER BY ordinal_position"
    )
    result = connection.exec_driver_sql(statement, (schema, table))
    columns: list[tuple[str, str, bool]] = []
    for row in result:
        if len(columns) >= MAX_COLUMNS:
            break
        columns.append((str(row[0]), str(row[1]), (str(row[2]).lower() == "yes")))
    return columns


def _probe_connection_impl(source: DatabaseSource) -> dict[str, str]:
    with _safe_connect(source) as (_engine, connection):
        if source.engine == "postgresql":
            row = connection.exec_driver_sql("SELECT version()").fetchone()
        else:
            row = connection.exec_driver_sql("SELECT VERSION()").fetchone()
        database = connection.exec_driver_sql(
            "SELECT current_database()"
            if source.engine == "postgresql"
            else "SELECT DATABASE()"
        ).fetchone()
        return {
            "server_version": str(row[0]) if row else "",
            "current_database": str(database[0]) if database else "",
        }


def _catalog_impl(source: DatabaseSource, schema: str) -> list[DatabaseTable]:
    schema = validate_identifier(schema, "schema")
    with _safe_connect(source) as (_engine, connection):
        if schema not in _list_schemas_impl_from_connection(connection, source.engine):
            raise DatabaseSourceError("schema_not_found", "远程 Schema 不存在")
        tables = _list_tables_impl(connection, source.engine, schema)
        pks_by_table = {
            table: _fetch_primary_keys_impl(connection, source.engine, schema, table)
            for table, _kind in tables
        }
        result: list[DatabaseTable] = []
        for table, kind in tables:
            columns = _fetch_columns_impl(connection, source.engine, schema, table)
            pks = pks_by_table.get(table, set())
            result.append(
                DatabaseTable(
                    schema_name=schema,
                    table_name=table,
                    kind="view" if kind.upper() == "VIEW" else "table",
                    columns=tuple(
                        DatabaseColumn(
                            name=name,
                            data_type=data_type,
                            nullable=nullable,
                            is_primary_key=name in pks,
                        )
                        for name, data_type, nullable in columns
                    ),
                )
            )
        return result


def _safe_text(value: str, max_cell_size: int) -> str:
    # PostgreSQL text/JSONB rejects U+0000 even when the remote database
    # considers it a valid character. Preserve readability without allowing
    # one control byte to abort the entire snapshot.
    return value.replace("\x00", "�")[:max_cell_size]


def _truncate_cell(
    value: Any,
    max_cell_size: int,
    *,
    data_type: str = "",
) -> Any:
    if value is None:
        return None
    normalized_type = data_type.lower().split("(", 1)[0].strip()
    if isinstance(value, (bytes, bytearray, memoryview)):
        payload = bytes(value)
        if normalized_type in BIT_DATA_TYPES:
            return int.from_bytes(payload, byteorder="big", signed=False)
        # Raw binary is unsuitable for tabular QA. A stable descriptor keeps
        # provenance without injecting NULs or huge Base64 strings.
        digest = hashlib.sha256(payload).hexdigest()
        return f"[binary {len(payload)} bytes sha256:{digest}]"
    if isinstance(value, str):
        return _safe_text(value, max_cell_size)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (int, float, bool)):
        return value
    return _safe_text(str(value), max_cell_size)


def _stream_table_impl(
    source: DatabaseSource,
    schema: str,
    table: str,
    *,
    max_rows: int = MAX_IMPORT_ROWS,
    max_cell_size: int = MAX_CELL_SIZE,
) -> tuple[list[str], list[dict[str, Any]]]:
    schema = validate_identifier(schema, "schema")
    table = validate_identifier(table, "表格")
    with _safe_connect(source) as (_engine, connection):
        if schema not in _list_schemas_impl_from_connection(connection, source.engine):
            raise DatabaseSourceError("schema_not_found", "远程 Schema 不存在")
        table_names = {
            name for name, _kind in _list_tables_impl(connection, source.engine, schema)
        }
        if table not in table_names:
            raise DatabaseSourceError("table_not_found", "远程表或视图不存在")
        columns = _fetch_columns_impl(connection, source.engine, schema, table)
        if not columns:
            raise DatabaseSourceError("table_not_found", "远程表没有可导入的列")
        column_names = [name for name, _data_type, _nullable in columns]
        column_types = {name: data_type for name, data_type, _nullable in columns}
        quoted_columns = ", ".join(
            _quote_driver_identifier(source.engine, name) for name in column_names
        )
        qualified = (
            f"{_quote_driver_identifier(source.engine, schema)}."
            f"{_quote_driver_identifier(source.engine, table)}"
        )
        sql = f"SELECT {quoted_columns} FROM {qualified}"
        result = connection.exec_driver_sql(sql)
        rows: list[dict[str, Any]] = []
        while True:
            batch = result.fetchmany(1_000)
            if not batch:
                break
            for raw in batch:
                if len(rows) >= max_rows:
                    raise DatabaseSourceError(
                        "row_limit_exceeded",
                        f"远程表超过首版单表 {max_rows} 行上限，请先建立筛选视图",
                    )
                rows.append(
                    {
                        name: _truncate_cell(
                            value,
                            max_cell_size,
                            data_type=column_types.get(name, ""),
                        )
                        for name, value in zip(column_names, raw)
                    }
                )
        return column_names, rows


# --------------------------------------------------------------------------
# Public async service boundary (event-loop safe)
# --------------------------------------------------------------------------


def _public_engine(engine: str) -> str:
    if engine not in _ENGINE_VALUES_PUBLIC:
        raise DatabaseSourceError("unsupported_engine", f"不支持的数据库类型：{engine}")
    return engine


def _document_external_identity(source_id: int, schema: str, table: str) -> str:
    canonical = f"database:{source_id}:{schema}:{table}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fingerprint_payload(columns: Sequence[str], rows: Sequence[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update("\u0000".join(columns).encode("utf-8"))
    # SQL does not guarantee row order without ORDER BY. Sort compact row
    # digests so an unchanged table is stable across different query plans.
    row_hashes: list[bytes] = []
    for row in rows:
        serialized = json.dumps(
            [row.get(name) for name in columns],
            ensure_ascii=False,
            sort_keys=False,
            default=str,
        )
        row_hashes.append(hashlib.sha256(serialized.encode("utf-8")).digest())
    for row_hash in sorted(row_hashes):
        digest.update(row_hash)
    return digest.hexdigest()


def _to_inferred_type(raw_values: Sequence[Any]) -> str:
    samples: list[Any] = []
    for value in raw_values:
        if value is None:
            continue
        samples.append(value)
        if len(samples) >= 64:
            break
    if not samples:
        return "text"
    if all(isinstance(item, bool) for item in samples):
        return "boolean"
    if all(isinstance(item, (int, float)) for item in samples):
        return "number"
    if all(isinstance(item, (datetime, date, time)) for item in samples):
        return "date"
    return "text"


def _build_sample_values(
    column_name: str,
    rows: Sequence[dict[str, Any]],
    *,
    limit: int = 8,
) -> list[str]:
    samples: list[str] = []
    seen: set[str] = set()
    for row in rows:
        value = row.get(column_name)
        if value is None:
            continue
        rendered = str(value)
        if rendered in seen:
            continue
        seen.add(rendered)
        samples.append(rendered)
        if len(samples) >= limit:
            break
    return samples


async def test_database_connection(source: DatabaseSource) -> dict[str, Any]:
    """Open a short-lived read-only session and report server metadata."""
    try:
        return await asyncio.to_thread(_probe_connection_impl, source)
    except DatabaseSourceError:
        raise
    except Exception as exc:
        raise DatabaseSourceError("connection_failed", f"连接失败：{exc}") from exc


async def list_database_schemas(source: DatabaseSource) -> list[str]:
    try:
        return await asyncio.to_thread(_list_schemas_impl, source)
    except DatabaseSourceError:
        raise
    except Exception as exc:
        raise DatabaseSourceError(
            "connection_failed", f"读取 Schema 失败：{exc}"
        ) from exc


async def fetch_database_catalog(
    source: DatabaseSource, schema: str
) -> list[DatabaseTable]:
    try:
        return await asyncio.to_thread(_catalog_impl, source, schema)
    except DatabaseSourceError:
        raise
    except Exception as exc:
        raise DatabaseSourceError(
            "connection_failed", f"读取表结构失败：{exc}"
        ) from exc


async def fetch_database_table(
    source: DatabaseSource, schema: str, table: str
) -> tuple[list[str], list[dict[str, Any]]]:
    try:
        return await asyncio.to_thread(_stream_table_impl, source, schema, table)
    except DatabaseSourceError:
        raise
    except Exception as exc:
        raise DatabaseSourceError(
            "connection_failed", f"读取远程表失败：{exc}"
        ) from exc


# --------------------------------------------------------------------------
# Import flow: stable Document, new DocumentVersion, replaced dataset,
# columnar artifact, full transactional rollback.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DatabaseImportResult:
    document_id: int | None
    document_version_id: int | None
    dataset_id: int | None
    snapshot_id: int | None
    version_number: int
    row_count: int
    column_count: int
    fingerprint: str
    reused_document: bool
    status: str = "imported"
    skip_reason: str | None = None
    removed_existing: bool = False


@dataclass(frozen=True)
class DatabaseSnapshotCleanupResult:
    affected: int
    deleted_artifacts: int
    cleanup_warnings: tuple[str, ...] = ()


def _artifact_storage_path(artifact: Any, root: str | Path | None) -> Path:
    if not getattr(artifact, "storage_key", None):
        raise DatabaseSourceError("artifact_unavailable", "数据集列式产物尚未就绪")
    base = Path(root or core_settings.storage_path).resolve()
    path = (base / artifact.storage_key).resolve()
    if base != path and base not in path.parents:
        raise DatabaseSourceError("artifact_invalid", "数据集产物路径无效")
    return path


async def _purge_database_snapshots(
    db: AsyncSession,
    snapshots: Sequence[DatabaseSnapshot],
    *,
    storage_root: str | Path | None = None,
) -> DatabaseSnapshotCleanupResult:
    """Permanently remove generated database documents and Parquet files."""

    if not snapshots:
        return DatabaseSnapshotCleanupResult(affected=0, deleted_artifacts=0)
    document_ids = sorted({snapshot.document_id for snapshot in snapshots})
    dataset_ids = sorted({snapshot.dataset_id for snapshot in snapshots})
    version_ids = list(
        (
            await db.scalars(
                select(DocumentVersion.id).where(
                    DocumentVersion.document_id.in_(document_ids)
                )
            )
        ).all()
    )
    artifacts = list(
        (
            await db.scalars(
                select(DatasetArtifact).where(
                    DatasetArtifact.dataset_id.in_(dataset_ids)
                )
            )
        ).all()
    )
    artifact_paths: list[Path] = []
    warnings: list[str] = []
    for artifact in artifacts:
        try:
            artifact_paths.append(_artifact_storage_path(artifact, storage_root))
        except DatabaseSourceError as exc:
            warnings.append(str(exc))

    documents = list(
        (await db.scalars(select(Document).where(Document.id.in_(document_ids)))).all()
    )
    for document in documents:
        document.current_version_id = None
    await db.flush()
    await db.execute(
        delete(DatabaseSnapshot).where(
            DatabaseSnapshot.id.in_([snapshot.id for snapshot in snapshots])
        )
    )
    await db.execute(
        delete(DatasetArtifact).where(DatasetArtifact.dataset_id.in_(dataset_ids))
    )
    await db.execute(
        delete(DatasetField).where(DatasetField.dataset_id.in_(dataset_ids))
    )
    await db.execute(
        delete(StructuredTableRow).where(
            StructuredTableRow.dataset_id.in_(dataset_ids)
        )
    )
    await db.execute(
        delete(KnowledgeDataset).where(KnowledgeDataset.id.in_(dataset_ids))
    )
    await db.execute(
        delete(DocumentVersion).where(DocumentVersion.id.in_(version_ids))
    )
    await db.execute(delete(Document).where(Document.id.in_(document_ids)))
    await db.commit()

    deleted_artifacts = 0
    for artifact_path in artifact_paths:
        try:
            artifact_path.unlink()
            deleted_artifacts += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            warnings.append(f"{artifact_path.name} 清理失败：{type(exc).__name__}")
    return DatabaseSnapshotCleanupResult(
        affected=len(snapshots),
        deleted_artifacts=deleted_artifacts,
        cleanup_warnings=tuple(warnings),
    )


async def purge_empty_database_snapshots(
    db: AsyncSession,
    source: DatabaseSource,
    *,
    storage_root: str | Path | None = None,
) -> DatabaseSnapshotCleanupResult:
    snapshots = list(
        (
            await db.scalars(
                select(DatabaseSnapshot).where(
                    DatabaseSnapshot.source_id == source.id,
                    DatabaseSnapshot.row_count == 0,
                )
            )
        ).all()
    )
    return await _purge_database_snapshots(
        db,
        snapshots,
        storage_root=storage_root,
    )


async def import_database_table(
    db: AsyncSession,
    source: DatabaseSource,
    schema: str,
    table: str,
    *,
    storage_root: str | Path | None = None,
) -> DatabaseImportResult:
    """Import a remote table as a Document + KnowledgeDataset.

    Creates a stable ``Document`` per (source, schema, table) so re-imports
    replace the previous snapshot in place. Each call appends a new
    ``DocumentVersion`` and rebuilds the columnar artifact.

    On any failure (remote error, dataset build, commit) the database
    transaction is rolled back and any newly published parquet file is
    deleted. The previous active artifact, if any, remains queryable.
    """

    _public_engine(source.engine)
    schema_name = validate_identifier(schema, "schema")
    table_name = validate_identifier(table, "表格")
    external_identity = _document_external_identity(source.id, schema_name, table_name)

    # Fetch the remote snapshot on a worker thread so the loop is not blocked.
    try:
        columns, rows = await asyncio.to_thread(
            _stream_table_impl, source, schema_name, table_name
        )
    except DatabaseSourceError:
        raise
    except Exception as exc:
        raise DatabaseSourceError(
            "connection_failed", f"无法读取远程表：{exc}"
        ) from exc

    if not columns:
        raise DatabaseSourceError("table_not_found", "远程表没有可导入的列")
    fingerprint = _fingerprint_payload(columns, rows)

    if not rows:
        existing_snapshot = await db.scalar(
            select(DatabaseSnapshot).where(
                DatabaseSnapshot.source_id == source.id,
                DatabaseSnapshot.schema_name == schema_name,
                DatabaseSnapshot.table_name == table_name,
            )
        )
        removed_existing = existing_snapshot is not None
        source.status = "idle"
        source.last_error = None
        source.last_sync_at = datetime.now(timezone.utc)
        db.add(source)
        if existing_snapshot is not None:
            await _purge_database_snapshots(
                db,
                [existing_snapshot],
                storage_root=storage_root,
            )
        else:
            await db.commit()
        return DatabaseImportResult(
            document_id=None,
            document_version_id=None,
            dataset_id=None,
            snapshot_id=None,
            version_number=0,
            row_count=0,
            column_count=len(columns),
            fingerprint=fingerprint,
            reused_document=removed_existing,
            status="skipped",
            skip_reason="empty_table",
            removed_existing=removed_existing,
        )

    document = await db.scalar(
        select(Document).where(Document.external_identity == external_identity)
    )
    reused_document = document is not None
    if document is None:
        document = Document(
            title=f"{source.name}.{schema_name}.{table_name}"[:1024],
            source_type=DocumentSourceType.file,
            external_identity=external_identity,
            meta={
                "external_source": "database",
                "database_source_id": source.id,
                "database_source_name": source.name,
                "database_schema": schema_name,
                "database_table": table_name,
                "database_engine": source.engine,
            },
        )
        db.add(document)
        await db.flush()
        version_number = 1
    else:
        if document.is_deleted:
            raise DatabaseSourceError(
                "document_trashed", "该资料已在回收站中，请先恢复"
            )
        next_version = 1
        if document.current_version_id is not None:
            current = await db.get(DocumentVersion, document.current_version_id)
            if current is not None and current.document_id == document.id:
                next_version = current.version_number + 1
        version_number = next_version

    content_hash = fingerprint
    version = DocumentVersion(
        document_id=document.id,
        version_number=version_number,
        content_hash=content_hash,
        processing_status="ready",
        meta={
            "external_source": "database",
            "database_source_id": source.id,
            "database_source_name": source.name,
            "database_schema": schema_name,
            "database_table": table_name,
            "database_engine": source.engine,
            "row_count": len(rows),
            "column_count": len(columns),
            "snapshot_fingerprint": fingerprint,
        },
        structured_content={
            "document_type": "database_table",
            "metadata": {
                "spreadsheet_schema_version": 2,
                "source_format": "database",
                "catalog_source": "database",
            },
            "headers": list(columns),
            # Rows live once in StructuredTableRow and are projected to
            # Parquet for DuckDB. Keeping a second JSON copy here makes large
            # database snapshots needlessly expensive.
            "rows": [],
        },
        raw_content=(
            f"数据库快照：{source.name}.{schema_name}.{table_name}\n"
            f"{len(rows)} 行 × {len(columns)} 列\n"
            f"字段：{', '.join(columns)}"
        ),
    )
    db.add(version)
    await db.flush()
    document.current_version_id = version.id

    # Replace the previous dataset/rows in the same transaction. We delete the
    # children explicitly because the SQLite test backend (and some MySQL
    # deployments) does not enforce FK cascades on every path.
    old_datasets = list(
        (
            await db.scalars(
                select(KnowledgeDataset).where(
                    KnowledgeDataset.document_id == document.id
                )
            )
        ).all()
    )

    # Allocate the replacement before deleting old rows. Besides making the
    # transition easier to inspect, this prevents SQLite (used in tests and
    # lightweight installs) from reusing the old integer id and publishing
    # the new Parquet over the only rollback-safe copy.
    dataset = KnowledgeDataset(
        document_id=document.id,
        document_version_id=version.id,
        name=table_name[:512],
        sheet_name=table_name[:256],
        region_index=1,
        dataset_kind="table",
        status="ready",
        row_count=len(rows),
        column_count=len(columns),
        profile={
            "catalog_source": "database",
            "row_limit": MAX_IMPORT_ROWS,
            "engine": source.engine,
            "schema_name": schema_name,
            "table_name": table_name,
        },
    )
    db.add(dataset)
    await db.flush()

    old_dataset_ids = [old.id for old in old_datasets]
    old_artifact_paths: list[Path] = []
    if old_dataset_ids:
        old_artifacts = list(
            (
                await db.scalars(
                    select(DatasetArtifact).where(
                        DatasetArtifact.dataset_id.in_(old_dataset_ids)
                    )
                )
            ).all()
        )
        for old_artifact in old_artifacts:
            try:
                old_artifact_paths.append(
                    _artifact_storage_path(old_artifact, storage_root)
                )
            except DatabaseSourceError:
                pass
        await db.execute(
            delete(DatasetField).where(DatasetField.dataset_id.in_(old_dataset_ids))
        )
        await db.execute(
            delete(StructuredTableRow).where(
                StructuredTableRow.dataset_id.in_(old_dataset_ids)
            )
        )
        await db.execute(
            delete(DatasetArtifact).where(
                DatasetArtifact.dataset_id.in_(old_dataset_ids)
            )
        )
    for old in old_datasets:
        await db.delete(old)
    await db.flush()

    sample_pool: list[dict[str, Any]] = list(rows[:128])
    for index, name in enumerate(columns):
        column_values = [row.get(name) for row in sample_pool]
        inferred = _to_inferred_type(column_values)
        samples = _build_sample_values(name, sample_pool)
        null_count = sum(1 for row in rows if row.get(name) is None)
        distinct = len({str(value) for value in column_values if value is not None})
        db.add(
            DatasetField(
                dataset_id=dataset.id,
                position=index,
                name=name[:512],
                inferred_type=inferred,
                null_count=null_count,
                distinct_count=(distinct if len(rows) <= len(sample_pool) else None),
                sample_values=samples,
                statistics={
                    "profiled_rows": len(sample_pool),
                    "is_sampled": len(rows) > len(sample_pool),
                },
            )
        )

    for offset in range(0, len(rows), 2_000):
        row_records = [
            {
                "workspace_id": source.workspace_id,
                "document_id": document.id,
                "document_version_id": version.id,
                "dataset_id": dataset.id,
                "sheet_name": table_name[:256],
                "region_index": 1,
                "row_number": offset + index + 1,
                "values": row,
            }
            for index, row in enumerate(rows[offset : offset + 2_000])
        ]
        await db.execute(
            insert(StructuredTableRow),
            row_records,
        )

    existing_snapshot = await db.scalar(
        select(DatabaseSnapshot).where(
            DatabaseSnapshot.source_id == source.id,
            DatabaseSnapshot.schema_name == schema_name,
            DatabaseSnapshot.table_name == table_name,
        )
    )
    now = datetime.now(timezone.utc)
    if existing_snapshot is None:
        snapshot = DatabaseSnapshot(
            source_id=source.id,
            schema_name=schema_name,
            table_name=table_name,
            document_id=document.id,
            dataset_id=dataset.id,
            row_count=len(rows),
            snapshot_fingerprint=fingerprint,
            snapshot_at=now,
        )
        db.add(snapshot)
        await db.flush()
    else:
        existing_snapshot.document_id = document.id
        existing_snapshot.dataset_id = dataset.id
        existing_snapshot.row_count = len(rows)
        existing_snapshot.snapshot_fingerprint = fingerprint
        existing_snapshot.snapshot_at = now
        existing_snapshot.last_error = None
        snapshot = existing_snapshot

    source.status = "idle"
    source.last_error = None
    source.last_sync_at = now
    db.add(source)

    await db.flush()

    # Build the columnar artifact in a sync sub-greenthread so the Parquet
    # builder participates in the active transaction. ``run_sync`` keeps the
    # call on the same connection as the async session; without it the
    # builder would have to be re-written for the async engine.
    def _build(sync_session: Session) -> Any:
        return build_dataset_parquet(sync_session, dataset, storage_root=storage_root)

    try:
        artifact = await db.run_sync(_build)
    except DatasetExecutionError as exc:
        await db.rollback()
        raise DatabaseSourceError("artifact_build_failed", str(exc)) from exc
    except Exception as exc:
        await db.rollback()
        raise DatabaseSourceError("import_failed", f"导入失败：{exc}") from exc

    # If the commit fails the parquet file is already on disk; remove it so
    # the filesystem mirrors the database. The previous active artifact, if
    # any, was unpublished by ``build_dataset_parquet`` and is left alone.
    artifact_path = _artifact_storage_path(artifact, storage_root)
    try:
        await db.commit()
    except Exception as exc:
        try:
            artifact_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise DatabaseSourceError("import_failed", f"导入提交失败：{exc}") from exc

    # The new snapshot is durable now. Old artifacts can finally be removed;
    # keeping this after commit preserves the previous snapshot on rollback.
    for old_path in old_artifact_paths:
        if old_path == artifact_path:
            continue
        try:
            old_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    return DatabaseImportResult(
        document_id=document.id,
        document_version_id=version.id,
        dataset_id=dataset.id,
        snapshot_id=snapshot.id,
        version_number=version_number,
        row_count=len(rows),
        column_count=len(columns),
        fingerprint=fingerprint,
        reused_document=reused_document,
    )


async def delete_database_source_documents(
    db: AsyncSession,
    source: DatabaseSource,
    *,
    document_action: str,
) -> list[int]:
    """Return the document ids associated with ``source``.

    ``document_action`` must be ``"keep"`` or ``"trash"``; the caller is
    responsible for applying the chosen lifecycle to the returned ids.
    """

    if document_action not in {"keep", "trash"}:
        raise DatabaseSourceError(
            "invalid_action", f"不支持的删除动作：{document_action}"
        )
    snapshots = list(
        (
            await db.scalars(
                select(DatabaseSnapshot).where(DatabaseSnapshot.source_id == source.id)
            )
        ).all()
    )
    document_ids = sorted(
        {
            snapshot.document_id
            for snapshot in snapshots
            if snapshot.document_id is not None
        }
    )
    return document_ids


__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_ENGINE_PORT",
    "ENGINE_DRIVER",
    "MAX_CATALOG_TABLES",
    "MAX_CELL_SIZE",
    "MAX_COLUMNS",
    "MAX_IMPORT_ROWS",
    "MAX_SCHEMAS",
    "STATEMENT_TIMEOUT_MS",
    "DatabaseColumn",
    "DatabaseImportResult",
    "DatabaseSnapshotCleanupResult",
    "DatabaseSourceError",
    "DatabaseTable",
    "delete_database_source_documents",
    "fetch_database_catalog",
    "fetch_database_table",
    "import_database_table",
    "list_database_schemas",
    "purge_empty_database_snapshots",
    "test_database_connection",
    "validate_database_host",
    "validate_identifier",
    "validate_ssl_mode",
]
