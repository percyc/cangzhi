"""Tests for the read-only database source MVP.

The driver layer (SQLAlchemy, sockets, DuckDB) is replaced with monkey-patched
fakes so the tests never touch a real database server. The HTTP layer is
exercised through the shared ``client`` fixture; service-level paths are
covered by calling :func:`apps.api.services.database_source.import_database_table`
directly with a stubbed driver.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.chunks import DocumentChunk
from apps.api.models.database_source import DatabaseSnapshot, DatabaseSource
from apps.api.models.datasets import DatasetArtifact, DatasetField, KnowledgeDataset
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.api.models.table_rows import StructuredTableRow
from apps.api.models.taxonomy import DocumentCategory, DocumentTag, Tag
from apps.api.models.workspaces import DEFAULT_WORKSPACE_SLUG, Workspace
from apps.api.security.secrets import (
    decrypt_secret,
    encrypt_secret,
)
from apps.api.services import database_source as db_source_module
from apps.api.services.database_source import (
    DatabaseSourceError,
    validate_database_host,
)
from apps.api.services.dataset_execution import DatasetExecutionError
from apps.api.services.workspaces import (
    bind_workspace_context,
    clear_workspace_context,
)

# --------------------------------------------------------------------------
# Pure unit tests: host safety
# --------------------------------------------------------------------------


def test_validate_database_host_rejects_loopback_and_link_local():
    for bad in (
        "127.0.0.1",
        "::1",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
    ):
        with pytest.raises(DatabaseSourceError):
            validate_database_host(bad, resolve=False)

    # "localhost" and friends are name-blocked before resolution.
    with pytest.raises(DatabaseSourceError):
        validate_database_host("localhost", resolve=False)
    with pytest.raises(DatabaseSourceError):
        validate_database_host("api.localhost", resolve=False)


def test_validate_database_host_rejects_private_without_trust_flag():
    with pytest.raises(DatabaseSourceError) as exc:
        validate_database_host("10.0.0.5", resolve=False)
    assert exc.value.code == "host_private"
    with pytest.raises(DatabaseSourceError):
        validate_database_host("192.168.10.20", resolve=False)
    with pytest.raises(DatabaseSourceError):
        validate_database_host("172.16.0.1", resolve=False)


def test_validate_database_host_allows_private_when_trusted_and_global_by_default():
    assert (
        validate_database_host("10.0.0.5", resolve=False, trusted_private_network=True)
        == "10.0.0.5"
    )
    assert validate_database_host("8.8.8.8", resolve=False) == "8.8.8.8"


def test_validate_database_host_rejects_control_characters():
    with pytest.raises(DatabaseSourceError):
        validate_database_host("exa\nmple.com", resolve=False)
    with pytest.raises(DatabaseSourceError):
        validate_database_host("exa\x00mple.com", resolve=False)


def test_validate_identifier_supports_quoted_unicode_and_rejects_controls():
    assert db_source_module.validate_identifier("销售 数据", "表格") == "销售 数据"
    assert db_source_module.validate_identifier("2026报表", "表格") == "2026报表"
    with pytest.raises(DatabaseSourceError):
        db_source_module.validate_identifier("drop\x00table", "表格")
    with pytest.raises(DatabaseSourceError):
        db_source_module.validate_identifier("   ", "schema")


def test_snapshot_fingerprint_is_stable_when_database_row_order_changes():
    columns = ["id", "name"]
    first = [{"id": 1, "name": "甲"}, {"id": 2, "name": "乙"}]
    second = list(reversed(first))
    assert db_source_module._fingerprint_payload(
        columns, first
    ) == db_source_module._fingerprint_payload(columns, second)


# --------------------------------------------------------------------------
# Driver fakes
# --------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: Sequence[Sequence[Any]]):
        self._rows = list(rows)
        self.description = tuple(("col",) for _ in (rows[0] if rows else ())) or (
            ("col",),
        )

    def __iter__(self):
        return iter(self.fetchall())

    def fetchall(self):
        rows = list(self._rows)
        self._rows = []
        return rows

    def fetchone(self):
        if not self._rows:
            return None
        return self._rows.pop(0)

    def fetchmany(self, size: int):
        if not self._rows:
            return []
        batch = self._rows[:size]
        self._rows = self._rows[size:]
        return batch


class _FakeConnection:
    """In-memory stand-in for the SQLAlchemy connection the driver uses.

    Only the queries the service code actually emits are recognised; anything
    else returns an empty result so the test fails loudly if the service
    starts running untrusted SQL.
    """

    def __init__(
        self,
        schema_rows: Sequence[Sequence[Any]],
        table_rows: Sequence[Sequence[Any]],
        columns_rows: Sequence[Sequence[Any]],
        fetch_rows: Sequence[Sequence[Any]],
        server_version: str = "PG 16.0",
        database: str = "warehouse",
    ) -> None:
        self._schema_rows = list(schema_rows)
        self._table_rows = list(table_rows)
        self._columns_rows = list(columns_rows)
        self._fetch_rows = list(fetch_rows)
        self._server_version = server_version
        self._database = database
        self.calls: list[tuple[str, tuple]] = []

    def exec_driver_sql(self, sql: str, params=None):
        self.calls.append((sql, tuple(params) if params else ()))
        normalized = " ".join(sql.split()).lower()
        if "from information_schema.schemata" in normalized:
            return _FakeResult(self._schema_rows)
        if "from information_schema.tables" in normalized:
            return _FakeResult(self._table_rows)
        if "from information_schema.columns" in normalized:
            return _FakeResult(self._columns_rows)
        if "table_constraints" in normalized and "key_column_usage" in normalized:
            return _FakeResult([])
        if sql.strip().lower().startswith("select version"):
            return _FakeResult([(self._server_version,)])
        if "current_database" in sql.lower() or "database()" in sql.lower():
            return _FakeResult([(self._database,)])
        if sql.strip().lower().startswith("select "):
            return _FakeResult(self._fetch_rows)
        return _FakeResult([])


def _install_fake_driver(
    monkeypatch,
    *,
    schemas: Sequence[str] = ("public",),
    tables: Sequence[str] = ("orders",),
    columns: Sequence[Sequence[str]] = (
        ("id", "integer", "NO"),
        ("name", "text", "YES"),
    ),
    rows: Sequence[Sequence[Any]] = (),
    server_version: str = "PG 16.0",
    database: str = "warehouse",
):
    fake_conn = _FakeConnection(
        [(*row,) for row in ((name,) for name in schemas)],
        [(name, "BASE TABLE") for name in tables],
        list(columns),
        list(rows),
        server_version=server_version,
        database=database,
    )

    @contextmanager
    def fake_safe_connect(_source):
        yield (None, fake_conn)

    monkeypatch.setattr(db_source_module, "_safe_connect", fake_safe_connect)
    return fake_conn


# --------------------------------------------------------------------------
# Service-level: catalog with fake driver
# --------------------------------------------------------------------------


def test_list_schemas_uses_fake_driver(monkeypatch):
    _install_fake_driver(monkeypatch, schemas=("public", "audit"))
    source = db_source_module.DatabaseSource(
        id=1,
        name="研发库",
        engine="postgresql",
        host="db.example.com",
        port=5432,
        database_name="warehouse",
        username="reader",
        password_cipher=encrypt_secret("secret"),
        has_password=True,
        ssl_mode="prefer",
        trusted_private_network=False,
        is_enabled=True,
        status="idle",
    )
    schemas = asyncio.run(db_source_module.list_database_schemas(source))
    assert schemas == ["public", "audit"]


def test_mysql_schema_listing_is_limited_to_configured_database(monkeypatch):
    connection = _install_fake_driver(monkeypatch, schemas=("warehouse",))
    source = db_source_module.DatabaseSource(
        id=1,
        name="MySQL 业务库",
        engine="mysql",
        host="db.example.com",
        port=3306,
        database_name="warehouse",
        username="reader",
        password_cipher=encrypt_secret("secret"),
        has_password=True,
        ssl_mode="required",
        trusted_private_network=False,
        is_enabled=True,
        status="idle",
    )

    schemas = asyncio.run(db_source_module.list_database_schemas(source))

    assert schemas == ["warehouse"]
    schema_query = next(
        call for call in connection.calls if "information_schema.schemata" in call[0]
    )
    assert "schema_name = %s" in schema_query[0].lower()
    assert schema_query[1] == ("warehouse",)


def test_fetch_catalog_returns_tables_and_columns(monkeypatch):
    _install_fake_driver(
        monkeypatch,
        tables=("orders", "users"),
        columns=(
            ("id", "integer", "NO"),
            ("email", "text", "YES"),
        ),
    )
    source = db_source_module.DatabaseSource(
        id=1,
        name="研发库",
        engine="postgresql",
        host="db.example.com",
        port=5432,
        database_name="warehouse",
        username="reader",
        password_cipher=encrypt_secret("secret"),
        has_password=True,
        ssl_mode="prefer",
        trusted_private_network=False,
        is_enabled=True,
        status="idle",
    )
    tables = asyncio.run(db_source_module.fetch_database_catalog(source, "public"))
    assert [item.table_name for item in tables] == ["orders", "users"]
    assert tables[0].kind == "table"
    assert [column.name for column in tables[0].columns] == ["id", "email"]


def test_fetch_table_data_returns_rows_with_truncation(monkeypatch):
    long_text = "x" * (db_source_module.MAX_CELL_SIZE + 50)
    _install_fake_driver(
        monkeypatch,
        rows=[(1, "alpha"), (2, long_text)],
    )
    source = db_source_module.DatabaseSource(
        id=1,
        name="研发库",
        engine="postgresql",
        host="db.example.com",
        port=5432,
        database_name="warehouse",
        username="reader",
        password_cipher=encrypt_secret("secret"),
        has_password=True,
        ssl_mode="prefer",
        trusted_private_network=False,
        is_enabled=True,
        status="idle",
    )
    columns, rows = asyncio.run(
        db_source_module.fetch_database_table(source, "public", "orders")
    )
    assert columns == ["id", "name"]
    assert rows[0] == {"id": 1, "name": "alpha"}
    assert rows[1]["name"] == "x" * db_source_module.MAX_CELL_SIZE


def test_fetch_table_normalizes_bit_blob_and_nul_text(monkeypatch):
    _install_fake_driver(
        monkeypatch,
        columns=(
            ("locked", "bit", "NO"),
            ("payload", "longblob", "YES"),
            ("note", "varchar", "YES"),
        ),
        rows=[(b"\x00", b"\x89PNG\x00payload", "a\x00b")],
    )
    source = db_source_module.DatabaseSource(
        id=1,
        name="研发库",
        engine="mysql",
        host="db.example.com",
        port=3306,
        database_name="warehouse",
        username="reader",
        password_cipher=encrypt_secret("secret"),
        has_password=True,
        ssl_mode="required",
        trusted_private_network=False,
        is_enabled=True,
        status="idle",
    )
    _, rows = asyncio.run(
        db_source_module.fetch_database_table(source, "public", "orders")
    )
    assert rows[0]["locked"] == 0
    assert rows[0]["payload"].startswith("[binary 12 bytes sha256:")
    assert "\x00" not in rows[0]["payload"]
    assert rows[0]["note"] == "a�b"


def test_fetch_table_rejects_unknown_table(monkeypatch):
    _install_fake_driver(monkeypatch, tables=("orders",))
    source = db_source_module.DatabaseSource(
        id=1,
        name="研发库",
        engine="postgresql",
        host="db.example.com",
        port=5432,
        database_name="warehouse",
        username="reader",
        password_cipher=encrypt_secret("secret"),
        has_password=True,
        ssl_mode="prefer",
        trusted_private_network=False,
        is_enabled=True,
        status="idle",
    )
    with pytest.raises(DatabaseSourceError) as exc:
        asyncio.run(db_source_module.fetch_database_table(source, "public", "missing"))
    assert exc.value.code == "table_not_found"


def test_test_connection_returns_server_metadata(monkeypatch):
    _install_fake_driver(monkeypatch, server_version="PG 16.4", database="warehouse")
    source = db_source_module.DatabaseSource(
        id=1,
        name="研发库",
        engine="postgresql",
        host="db.example.com",
        port=5432,
        database_name="warehouse",
        username="reader",
        password_cipher=encrypt_secret("secret"),
        has_password=True,
        ssl_mode="prefer",
        trusted_private_network=False,
        is_enabled=True,
        status="idle",
    )
    info = asyncio.run(db_source_module.test_database_connection(source))
    assert info == {
        "server_version": "PG 16.4",
        "current_database": "warehouse",
    }


# --------------------------------------------------------------------------
# Helpers for the import flow
# --------------------------------------------------------------------------


def _seed_workspace(slug: str, name: str = "研究") -> int:
    async def go() -> int:
        generator = app.dependency_overrides[get_db]()
        session = await anext(generator)
        try:
            workspace = Workspace(
                slug=slug, name=name, is_default=False, status="active", settings={}
            )
            session.add(workspace)
            await session.commit()
            await session.refresh(workspace)
            return workspace.id
        finally:
            await generator.aclose()

    return asyncio.run(go())


def _query_counts() -> dict[str, int]:
    from sqlalchemy import func

    async def go() -> dict[str, int]:
        generator = app.dependency_overrides[get_db]()
        session = await anext(generator)
        try:
            bind_workspace_context(session.sync_session, 1, DEFAULT_WORKSPACE_SLUG)
            documents = int(
                (await session.execute(select(func.count(Document.id)))).scalar_one()
            )
            datasets = int(
                (
                    await session.execute(select(func.count(KnowledgeDataset.id)))
                ).scalar_one()
            )
            versions = int(
                (
                    await session.execute(select(func.count(DocumentVersion.id)))
                ).scalar_one()
            )
            snapshots = int(
                (
                    await session.execute(select(func.count(DatabaseSnapshot.id)))
                ).scalar_one()
            )
            return {
                "documents": documents,
                "datasets": datasets,
                "versions": versions,
                "snapshots": snapshots,
            }
        finally:
            clear_workspace_context(session.sync_session)
            await generator.aclose()

    return asyncio.run(go())


def _import_via_api(test_client, source_id: int):
    return test_client.post(
        f"/api/database-sources/{source_id}/tables/import",
        json={"schema_name": "public", "table": "orders"},
    )


def _create_source_payload(**overrides) -> dict:
    payload = {
        "name": "订单库",
        "engine": "postgresql",
        "host": "8.8.8.8",
        "port": 5432,
        "database_name": "warehouse",
        "username": "reader",
        "password": "secret",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# Import flow: first import + re-import + rollback
# --------------------------------------------------------------------------


def test_first_import_creates_document_dataset_and_artifact(
    client, monkeypatch, tmp_path
):
    test_client, _ = client
    _install_fake_driver(
        monkeypatch,
        rows=[(1, "alpha"), (2, "beta"), (3, "gamma")],
    )
    category_response = test_client.post(
        "/api/categories", json={"slug": "tech", "name": "技术与产品"}
    )
    assert category_response.status_code == 201
    category_id = category_response.json()["id"]
    created = test_client.post("/api/database-sources", json=_create_source_payload())
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]

    response = _import_via_api(test_client, source_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["reused_document"] is False
    assert body["version_number"] == 1
    assert body["row_count"] == 3
    assert body["column_count"] == 2
    overview = test_client.get(
        f"/api/documents/overview?limit=1&category_id={category_id}"
    )
    assert overview.status_code == 200
    assert overview.headers["X-Total-Count"] == "1"
    assert overview.json()[0]["primary_category"]["slug"] == "tech"

    document_id = body["document_id"]
    dataset_id = body["dataset_id"]
    parquet_path = (
        Path(tmp_path) / "storage" / "datasets" / str(dataset_id) / "v1.parquet"
    )
    assert parquet_path.is_file(), f"missing parquet at {parquet_path}"

    async def go():
        generator = app.dependency_overrides[get_db]()
        session = await anext(generator)
        try:
            bind_workspace_context(session.sync_session, 1, DEFAULT_WORKSPACE_SLUG)
            document = await session.get(Document, document_id)
            assert document is not None
            assert document.source_type == DocumentSourceType.file
            assert document.meta["external_source"] == "database"
            assert document.meta["database_source_id"] == source_id
            assert document.current_version_id is not None
            version = await session.get(DocumentVersion, document.current_version_id)
            assert version.version_number == 1
            assert version.content_hash == body["fingerprint"]
            assert version.structured_content["headers"] == ["id", "name"]
            assert version.structured_content["rows"] == []
            assert "3 行 × 2 列" in version.raw_content
            assert "alpha" not in version.raw_content
            fields = list(
                (
                    await session.scalars(
                        select(DatasetField).where(
                            DatasetField.dataset_id == dataset_id
                        )
                    )
                ).all()
            )
            assert [field.name for field in fields] == ["id", "name"]
            rows = list(
                (
                    await session.scalars(
                        select(StructuredTableRow)
                        .where(StructuredTableRow.dataset_id == dataset_id)
                        .order_by(StructuredTableRow.row_number)
                    )
                ).all()
            )
            assert [row.row_number for row in rows] == [1, 2, 3]
            active = await session.scalar(
                select(DatasetArtifact).where(
                    DatasetArtifact.dataset_id == dataset_id,
                    DatasetArtifact.is_active.is_(True),
                )
            )
            assert active is not None and active.status == "ready"
            snapshot = await session.scalar(
                select(DatabaseSnapshot).where(DatabaseSnapshot.source_id == source_id)
            )
            assert snapshot is not None
            assert snapshot.document_id == document_id
            assert snapshot.dataset_id == dataset_id
            assert snapshot.row_count == 3
            chunks = list(
                (
                    await session.scalars(
                        select(DocumentChunk)
                        .where(DocumentChunk.document_version_id == version.id)
                        .order_by(DocumentChunk.role.desc())
                    )
                ).all()
            )
            assert len(chunks) == 2
            child = next(chunk for chunk in chunks if chunk.role == "child")
            assert child.chunk_type == "dataset_catalog"
            assert "3 行，2 个字段" in child.content
            assert "id（number）" in child.content
            assert "alpha" not in child.content
            assert child.extra["dataset_id"] == dataset_id
            category_link = await session.scalar(
                select(DocumentCategory).where(
                    DocumentCategory.document_version_id == version.id
                )
            )
            assert category_link is not None
            assert category_link.source == "database"
            tag_names = set(
                (
                    await session.scalars(
                        select(Tag.name)
                        .join(DocumentTag, DocumentTag.tag_id == Tag.id)
                        .where(DocumentTag.document_version_id == version.id)
                    )
                ).all()
            )
            assert tag_names == {"数据库", "数据库表", "postgresql"}
        finally:
            clear_workspace_context(session.sync_session)
            await generator.aclose()

    asyncio.run(go())


def test_reimport_creates_new_version_and_replaces_dataset(
    client, monkeypatch, tmp_path
):
    test_client, _ = client
    _install_fake_driver(
        monkeypatch,
        rows=[(1, "alpha"), (2, "beta")],
    )
    source_id = test_client.post(
        "/api/database-sources", json=_create_source_payload()
    ).json()["id"]

    first = _import_via_api(test_client, source_id).json()
    first_dataset_id = first["dataset_id"]
    first_document_id = first["document_id"]
    custom_tag = test_client.post(
        "/api/tags", json={"slug": "business-data", "name": "业务数据"}
    )
    assert custom_tag.status_code == 201
    updated_tags = test_client.patch(
        f"/api/documents/{first_document_id}/tags",
        json={"tag_ids": [custom_tag.json()["id"]]},
    )
    assert updated_tags.status_code == 200
    first_path = (
        Path(tmp_path) / "storage" / "datasets" / str(first_dataset_id) / "v1.parquet"
    )
    assert first_path.is_file()

    _install_fake_driver(
        monkeypatch,
        rows=[(10, "zeta"), (20, "eta"), (30, "theta")],
    )
    second_response = _import_via_api(test_client, source_id)
    assert second_response.status_code == 201, second_response.text
    second = second_response.json()
    assert second["reused_document"] is True
    assert second["document_id"] == first_document_id
    assert second["version_number"] == 2
    assert second["row_count"] == 3

    async def go():
        generator = app.dependency_overrides[get_db]()
        session = await anext(generator)
        try:
            bind_workspace_context(session.sync_session, 1, DEFAULT_WORKSPACE_SLUG)
            document = await session.get(Document, first_document_id)
            current = await session.get(DocumentVersion, document.current_version_id)
            assert current.version_number == 2
            datasets = list(
                (
                    await session.scalars(
                        select(KnowledgeDataset).where(
                            KnowledgeDataset.document_id == document.id
                        )
                    )
                ).all()
            )
            assert len(datasets) == 1
            assert datasets[0].id == second["dataset_id"]
            rows = list(
                (
                    await session.scalars(
                        select(StructuredTableRow)
                        .where(StructuredTableRow.dataset_id == datasets[0].id)
                        .order_by(StructuredTableRow.row_number)
                    )
                ).all()
            )
            assert [row.values["id"] for row in rows] == [10, 20, 30]
            # Only one snapshot per (source, schema, table).
            snapshots = list(
                (
                    await session.scalars(
                        select(DatabaseSnapshot).where(
                            DatabaseSnapshot.source_id == source_id
                        )
                    )
                ).all()
            )
            assert len(snapshots) == 1
            tag_names = set(
                (
                    await session.scalars(
                        select(Tag.name)
                        .join(DocumentTag, DocumentTag.tag_id == Tag.id)
                        .where(DocumentTag.document_version_id == current.id)
                    )
                ).all()
            )
            assert tag_names == {"业务数据", "数据库", "数据库表", "postgresql"}
        finally:
            clear_workspace_context(session.sync_session)
            await generator.aclose()

    asyncio.run(go())
    new_path = (
        Path(tmp_path)
        / "storage"
        / "datasets"
        / str(second["dataset_id"])
        / "v1.parquet"
    )
    assert new_path.is_file()
    assert not first_path.exists(), "re-import should remove the superseded parquet"


@pytest.mark.parametrize(
    ("semantic_mode", "expected_pending"),
    [("smart", 0), ("full", 2)],
)
def test_reimport_reuses_unchanged_field_semantics_and_bounds_ai_work(
    client, monkeypatch, semantic_mode, expected_pending
):
    test_client, _ = client
    _install_fake_driver(monkeypatch, rows=[(1, "alpha"), (2, "beta")])
    created = test_client.post(
        "/api/database-sources",
        json=_create_source_payload(semantic_refresh_mode=semantic_mode),
    )
    assert created.status_code == 201
    assert created.json()["semantic_refresh_mode"] == semantic_mode
    source_id = created.json()["id"]
    first = _import_via_api(test_client, source_id).json()

    async def add_semantics():
        generator = app.dependency_overrides[get_db]()
        session = await anext(generator)
        try:
            fields = list(
                (
                    await session.scalars(
                        select(DatasetField)
                        .where(DatasetField.dataset_id == first["dataset_id"])
                        .order_by(DatasetField.position)
                    )
                ).all()
            )
            for field in fields:
                field.description = f"{field.name} 的既有说明"
                field.unit = "个" if field.name == "id" else None
                field.aliases = [f"{field.name}别名"]
                field.semantic_source = "ai"
                field.semantic_confidence = 0.9
            await session.commit()
        finally:
            await generator.aclose()

    asyncio.run(add_semantics())
    _install_fake_driver(monkeypatch, rows=[(10, "gamma"), (20, "delta")])
    second = _import_via_api(test_client, source_id).json()

    async def inspect_refresh():
        generator = app.dependency_overrides[get_db]()
        session = await anext(generator)
        try:
            dataset = await session.get(KnowledgeDataset, second["dataset_id"])
            fields = list(
                (
                    await session.scalars(
                        select(DatasetField)
                        .where(DatasetField.dataset_id == dataset.id)
                        .order_by(DatasetField.position)
                    )
                ).all()
            )
            job = await session.scalar(
                select(ProcessingJob).where(
                    ProcessingJob.document_version_id
                    == second["document_version_id"],
                    ProcessingJob.stage == "dataset_semantics",
                )
            )
            return dataset.profile, fields, job
        finally:
            await generator.aclose()

    profile, fields, job = asyncio.run(inspect_refresh())
    assert [field.description for field in fields] == [
        "id 的既有说明",
        "name 的既有说明",
    ]
    assert fields[0].unit == "个"
    assert fields[1].aliases == ["name别名"]
    assert profile["field_semantics_refresh"] == {
        "mode": semantic_mode,
        "status": "reused" if expected_pending == 0 else "pending",
        "reused_fields": 2,
        "estimated_ai_fields": expected_pending,
        "total_fields": 2,
    }
    assert (job is not None) is (expected_pending > 0)


def test_empty_table_is_skipped_and_removes_previous_snapshot(
    client, monkeypatch, tmp_path
):
    test_client, _ = client
    _install_fake_driver(monkeypatch, rows=[(1, "alpha")])
    source_id = test_client.post(
        "/api/database-sources", json=_create_source_payload()
    ).json()["id"]
    first = _import_via_api(test_client, source_id).json()
    artifact_path = (
        Path(tmp_path)
        / "storage"
        / "datasets"
        / str(first["dataset_id"])
        / "v1.parquet"
    )
    assert artifact_path.is_file()

    _install_fake_driver(monkeypatch, rows=[])
    response = _import_via_api(test_client, source_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "skipped"
    assert body["skip_reason"] == "empty_table"
    assert body["removed_existing"] is True
    assert body["document_id"] is None
    assert body["dataset_id"] is None
    assert _query_counts() == {
        "documents": 0,
        "datasets": 0,
        "versions": 0,
        "snapshots": 0,
    }
    assert not artifact_path.exists()


def test_failed_import_rolls_back_db_and_storage(client, monkeypatch, tmp_path):
    test_client, _ = client
    _install_fake_driver(monkeypatch, rows=[(1, "alpha")])
    source_id = test_client.post(
        "/api/database-sources", json=_create_source_payload()
    ).json()["id"]

    def fail_build(*_args, **_kwargs):
        raise DatasetExecutionError(
            "artifact_build_failed", "列式产物构建失败：fake outage"
        )

    monkeypatch.setattr(
        "apps.api.services.database_source.build_dataset_parquet", fail_build
    )

    response = _import_via_api(test_client, source_id)
    assert response.status_code == 502
    assert "fake outage" in response.json()["detail"]

    counts = _query_counts()
    assert counts == {"documents": 0, "datasets": 0, "versions": 0, "snapshots": 0}

    datasets_dir = Path(tmp_path) / "storage" / "datasets"
    if datasets_dir.exists():
        assert list(datasets_dir.rglob("*.parquet")) == []


def test_failed_reimport_preserves_previous_dataset_and_parquet(
    client, monkeypatch, tmp_path
):
    test_client, _ = client
    _install_fake_driver(monkeypatch, rows=[(1, "alpha")])
    source_id = test_client.post(
        "/api/database-sources", json=_create_source_payload()
    ).json()["id"]
    first = _import_via_api(test_client, source_id).json()
    first_path = (
        Path(tmp_path)
        / "storage"
        / "datasets"
        / str(first["dataset_id"])
        / "v1.parquet"
    )
    first_bytes = first_path.read_bytes()

    _install_fake_driver(monkeypatch, rows=[(2, "beta")])

    def fail_build(*_args, **_kwargs):
        raise DatasetExecutionError(
            "artifact_build_failed", "列式产物构建失败：reimport outage"
        )

    monkeypatch.setattr(
        "apps.api.services.database_source.build_dataset_parquet", fail_build
    )
    response = _import_via_api(test_client, source_id)
    assert response.status_code == 502
    assert first_path.read_bytes() == first_bytes

    async def go():
        generator = app.dependency_overrides[get_db]()
        session = await anext(generator)
        try:
            bind_workspace_context(session.sync_session, 1, DEFAULT_WORKSPACE_SLUG)
            datasets = list(
                (
                    await session.scalars(
                        select(KnowledgeDataset).where(
                            KnowledgeDataset.id == first["dataset_id"]
                        )
                    )
                ).all()
            )
            assert len(datasets) == 1
        finally:
            clear_workspace_context(session.sync_session)
            await generator.aclose()

    asyncio.run(go())


def test_failed_commit_cleans_up_published_artifact(client, monkeypatch, tmp_path):
    test_client, _ = client
    _install_fake_driver(monkeypatch, rows=[(1, "alpha")])
    source_id = test_client.post(
        "/api/database-sources", json=_create_source_payload()
    ).json()["id"]

    from sqlalchemy.ext.asyncio import AsyncSession

    original_commit = AsyncSession.commit
    state = {"calls": 0}

    async def failing_commit(self, *args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("simulated commit failure")
        return await original_commit(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "commit", failing_commit)

    response = _import_via_api(test_client, source_id)
    assert response.status_code == 502
    assert "导入提交失败" in response.json()["detail"]

    counts = _query_counts()
    assert counts == {"documents": 0, "datasets": 0, "versions": 0, "snapshots": 0}

    datasets_dir = Path(tmp_path) / "storage" / "datasets"
    if datasets_dir.exists():
        assert list(datasets_dir.rglob("*.parquet")) == []


def test_disabled_source_blocks_import(client, monkeypatch):
    test_client, _ = client
    _install_fake_driver(monkeypatch, rows=[(1, "alpha")])
    source_id = test_client.post(
        "/api/database-sources", json=_create_source_payload()
    ).json()["id"]
    test_client.patch(
        f"/api/database-sources/{source_id}",
        json={
            "name": "订单库",
            "host": "8.8.8.8",
            "port": 5432,
            "database_name": "warehouse",
            "username": "reader",
            "is_enabled": False,
        },
    )
    response = _import_via_api(test_client, source_id)
    assert response.status_code == 409


def test_import_rejects_invalid_identifier(client, monkeypatch):
    test_client, _ = client
    _install_fake_driver(monkeypatch, rows=[(1, "alpha")])
    source_id = test_client.post(
        "/api/database-sources", json=_create_source_payload()
    ).json()["id"]
    response = test_client.post(
        f"/api/database-sources/{source_id}/tables/import",
        json={"schema_name": "public", "table": "drop\u0000orders"},
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------
# Workspace isolation
# --------------------------------------------------------------------------


def test_database_sources_are_isolated_by_workspace(client, monkeypatch):
    test_client, _ = client
    _install_fake_driver(monkeypatch, rows=[(1, "alpha")])
    _seed_workspace("research", name="研究")

    created = test_client.post(
        "/api/database-sources",
        json=_create_source_payload(name="研究库"),
        headers={"X-Cangzhi-Workspace": "research"},
    )
    assert created.status_code == 201

    default_listing = test_client.get("/api/database-sources").json()
    research_listing = test_client.get(
        "/api/database-sources",
        headers={"X-Cangzhi-Workspace": "research"},
    ).json()
    assert default_listing == []
    assert len(research_listing) == 1

    cross_view = test_client.get(f"/api/database-sources/{created.json()['id']}")
    assert cross_view.status_code == 404

    cross_import = test_client.post(
        f"/api/database-sources/{created.json()['id']}/tables/import",
        json={"schema_name": "public", "table": "orders"},
    )
    assert cross_import.status_code == 404

    # Importing from the right workspace succeeds.
    _install_fake_driver(monkeypatch, rows=[(1, "alpha")])
    good_import = test_client.post(
        f"/api/database-sources/{created.json()['id']}/tables/import",
        json={"schema_name": "public", "table": "orders"},
        headers={"X-Cangzhi-Workspace": "research"},
    )
    assert good_import.status_code == 201


# --------------------------------------------------------------------------
# CRUD + password keep / replace / clear
# --------------------------------------------------------------------------


def _cipher_for(source_id: int) -> tuple[str, str]:
    async def go() -> tuple[str, str]:
        generator = app.dependency_overrides[get_db]()
        session = await anext(generator)
        try:
            bind_workspace_context(session.sync_session, 1, DEFAULT_WORKSPACE_SLUG)
            source = await session.get(DatabaseSource, source_id)
            return source.password_cipher, source.has_password
        finally:
            clear_workspace_context(session.sync_session)
            await generator.aclose()

    return asyncio.run(go())


def test_database_source_password_keep_replace_and_clear(client, monkeypatch):
    test_client, _ = client
    _install_fake_driver(monkeypatch, rows=[(1, "alpha")])

    created = test_client.post(
        "/api/database-sources",
        json=_create_source_payload(password="first-secret"),
    ).json()
    source_id = created["id"]
    cipher, has_password = _cipher_for(source_id)
    assert decrypt_secret(cipher) == "first-secret"
    assert has_password is True

    # No password payload + default action => keep the existing cipher.
    kept = test_client.patch(
        f"/api/database-sources/{source_id}",
        json={
            "name": "订单库",
            "host": "8.8.8.8",
            "port": 5432,
            "database_name": "warehouse",
            "username": "reader",
        },
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["has_password"] is True
    cipher, _ = _cipher_for(source_id)
    assert decrypt_secret(cipher) == "first-secret"

    # Replace with a new password.
    replaced = test_client.patch(
        f"/api/database-sources/{source_id}",
        json={
            "name": "订单库",
            "host": "8.8.8.8",
            "port": 5432,
            "database_name": "warehouse",
            "username": "reader",
            "password_action": "replace",
            "password": "second-secret",
        },
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["has_password"] is True
    cipher, _ = _cipher_for(source_id)
    assert decrypt_secret(cipher) == "second-secret"

    # Clear the password.
    cleared = test_client.patch(
        f"/api/database-sources/{source_id}",
        json={
            "name": "订单库",
            "host": "8.8.8.8",
            "port": 5432,
            "database_name": "warehouse",
            "username": "reader",
            "password_action": "clear",
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["has_password"] is False
    cipher, _ = _cipher_for(source_id)
    assert decrypt_secret(cipher) == ""

    # Inconsistent combination: keep + new password is rejected.
    conflict = test_client.patch(
        f"/api/database-sources/{source_id}",
        json={
            "name": "订单库",
            "host": "8.8.8.8",
            "port": 5432,
            "database_name": "warehouse",
            "username": "reader",
            "password_action": "keep",
            "password": "third-secret",
        },
    )
    assert conflict.status_code == 400


def test_database_source_response_never_exposes_cipher(client, monkeypatch):
    test_client, _ = client
    _install_fake_driver(monkeypatch)
    source_id = test_client.post(
        "/api/database-sources", json=_create_source_payload()
    ).json()["id"]
    detail = test_client.get(f"/api/database-sources/{source_id}").json()
    listing = test_client.get("/api/database-sources").json()
    for payload in (detail, listing[0]):
        assert "password_cipher" not in payload
        assert "password" not in payload
        assert "cipher" not in payload
        assert payload.get("has_password") is True


# --------------------------------------------------------------------------
# Delete-impact + delete with keep / trash
# --------------------------------------------------------------------------


def test_delete_impact_and_delete_keep_preserve_documents(client, monkeypatch):
    test_client, _ = client
    _install_fake_driver(monkeypatch, rows=[(1, "alpha"), (2, "beta")])
    source_id = test_client.post(
        "/api/database-sources", json=_create_source_payload()
    ).json()["id"]
    imported = _import_via_api(test_client, source_id).json()
    document_id = imported["document_id"]

    impact = test_client.get(f"/api/database-sources/{source_id}/delete-impact").json()
    assert impact["active_document_count"] == 1
    assert impact["trashed_document_count"] == 0
    assert impact["snapshot_count"] == 1

    deleted = test_client.delete(
        f"/api/database-sources/{source_id}?document_action=keep"
    )
    assert deleted.status_code == 200
    assert deleted.json()["affected_documents"] == 1
    assert deleted.json()["document_action"] == "keep"

    detail = test_client.get(f"/api/documents/{document_id}")
    assert detail.status_code == 200
    # Connector removal metadata is in the document's `origin` block and
    # surfaces to the UI through ``build_document_response``. Verify both
    # the structural fields and the database row directly.
    assert detail.json()["origin"] is None or detail.json()["origin"]["kind"] in {
        "database",
        None,
    }

    async def fetch_meta():
        generator = app.dependency_overrides[get_db]()
        session = await anext(generator)
        try:
            bind_workspace_context(session.sync_session, 1, DEFAULT_WORKSPACE_SLUG)
            document = await session.get(Document, document_id)
            return dict(document.meta or {})
        finally:
            clear_workspace_context(session.sync_session)
            await generator.aclose()

    meta = asyncio.run(fetch_meta())
    assert meta.get("connector_removed") is True
    assert meta.get("database_source_id") is None
    assert meta.get("database_source_name") == "订单库"
    assert test_client.get("/api/documents?deleted=true").json() == []


def test_delete_impact_and_delete_trash_move_documents_to_trash(client, monkeypatch):
    test_client, _ = client
    _install_fake_driver(monkeypatch, rows=[(1, "alpha")])
    source_id = test_client.post(
        "/api/database-sources", json=_create_source_payload()
    ).json()["id"]
    imported = _import_via_api(test_client, source_id).json()
    document_id = imported["document_id"]

    impact = test_client.get(f"/api/database-sources/{source_id}/delete-impact").json()
    assert impact["active_document_count"] == 1

    deleted = test_client.delete(
        f"/api/database-sources/{source_id}?document_action=trash"
    )
    assert deleted.status_code == 200
    assert deleted.json()["document_action"] == "trash"

    assert test_client.get(f"/api/database-sources/{source_id}").status_code == 404
    trashed = test_client.get("/api/documents?deleted=true").json()
    assert len(trashed) == 1
    assert trashed[0]["id"] == document_id
    assert trashed[0]["delete_reason"] == "connector_removed"


# --------------------------------------------------------------------------
# Host safety through the public API
# --------------------------------------------------------------------------


def test_create_source_rejects_loopback_and_private(client):
    test_client, _ = client
    response = test_client.post(
        "/api/database-sources",
        json=_create_source_payload(host="127.0.0.1"),
    )
    assert response.status_code == 400
    assert "本机" in response.json()["detail"]

    private = test_client.post(
        "/api/database-sources",
        json=_create_source_payload(host="10.0.0.1"),
    )
    assert private.status_code == 400
    assert "内网" in private.json()["detail"]

    trusted_private = test_client.post(
        "/api/database-sources",
        json=_create_source_payload(
            host="10.0.0.1", name="private-trusted", trusted_private_network=True
        ),
    )
    assert trusted_private.status_code == 201


def test_test_endpoint_reports_failure(monkeypatch, client):
    test_client, _ = client

    async def boom(_source):
        raise DatabaseSourceError("connection_failed", "无法连接数据库")

    from apps.api.api import database_sources as db_api_module

    monkeypatch.setattr(db_api_module, "test_database_connection", boom)

    source_id = test_client.post(
        "/api/database-sources", json=_create_source_payload()
    ).json()["id"]
    response = test_client.post(f"/api/database-sources/{source_id}/test")
    assert response.status_code == 502
    assert "无法连接" in response.json()["detail"]


def test_catalog_and_schemas_endpoints_use_fakes(client, monkeypatch):
    test_client, _ = client
    _install_fake_driver(
        monkeypatch,
        schemas=("public", "audit"),
        tables=("orders",),
        rows=[(1, "alpha")],
    )
    source_id = test_client.post(
        "/api/database-sources", json=_create_source_payload()
    ).json()["id"]

    schemas = test_client.get(f"/api/database-sources/{source_id}/schemas").json()
    assert schemas == ["public", "audit"]

    catalog = test_client.get(
        f"/api/database-sources/{source_id}/catalog?schema=public"
    ).json()
    assert len(catalog) == 1
    assert catalog[0]["table_name"] == "orders"
    assert catalog[0]["imported"] is False
    assert catalog[0]["snapshot"] is None
    assert [column["name"] for column in catalog[0]["columns"]] == ["id", "name"]

    imported = _import_via_api(test_client, source_id)
    assert imported.status_code == 201

    catalog = test_client.get(
        f"/api/database-sources/{source_id}/catalog?schema=public"
    ).json()
    assert catalog[0]["imported"] is True
    assert catalog[0]["snapshot"]["row_count"] == 1
    assert catalog[0]["snapshot"]["document_deleted"] is False

    snapshots = test_client.get(
        f"/api/database-sources/{source_id}/snapshots"
    )
    assert snapshots.status_code == 200
    assert [row["table_name"] for row in snapshots.json()] == ["orders"]

    changed_scope = test_client.patch(
        f"/api/database-sources/{source_id}",
        json={"database_name": "another_database"},
    )
    assert changed_scope.status_code == 409
    assert changed_scope.json()["detail"]["code"] == "database_scope_has_snapshots"
