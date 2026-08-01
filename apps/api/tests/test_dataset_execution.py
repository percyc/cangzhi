from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from apps.api.core.db import Base
from apps.api.models.datasets import DatasetArtifact, DatasetField, KnowledgeDataset
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.processing import ProcessingJob
from apps.api.models.table_rows import StructuredTableRow
from apps.api.services.dataset_execution import (
    DatasetExecutionError,
    build_dataset_parquet,
    discover_dataset_literals,
    execute_dataset_query,
)
from apps.worker.services.processor import _process_dataset_artifacts


def _seed_and_build(path: Path) -> tuple[int, int]:
    engine = create_engine(f"sqlite:///{path / 'builder.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        document = Document(title="销售数据", source_type=DocumentSourceType.file)
        session.add(document)
        session.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            content_hash="dataset-engine",
            processing_status="ready",
        )
        session.add(version)
        session.flush()
        document.current_version_id = version.id
        dataset = KnowledgeDataset(
            document_id=document.id,
            document_version_id=version.id,
            name="销售",
            sheet_name="销售",
            region_index=1,
            status="ready",
            row_count=3,
            column_count=3,
            profile={},
        )
        session.add(dataset)
        session.flush()
        session.add_all(
            [
                DatasetField(
                    dataset_id=dataset.id,
                    position=0,
                    name="地区",
                    inferred_type="text",
                    sample_values=["华东", "全国-华东", "全国-华南"],
                ),
                DatasetField(
                    dataset_id=dataset.id,
                    position=1,
                    name="金额",
                    inferred_type="number",
                ),
                DatasetField(
                    dataset_id=dataset.id, position=2, name="日期", inferred_type="date"
                ),
            ]
        )
        for number, region, amount in [
            (2, "华东", "10.0（原始值：10）"),
            (3, "华东", "20"),
            (4, "华南", "7"),
            (5, "全国-华东", "30"),
            (6, "全国-华南", "40"),
            (7, "全国-华东-上海", "99"),
        ]:
            session.add(
                StructuredTableRow(
                    document_id=document.id,
                    document_version_id=version.id,
                    dataset_id=dataset.id,
                    sheet_name="销售",
                    region_index=1,
                    row_number=number,
                    values={"地区": region, "金额": amount, "日期": "2026-07-31"},
                )
            )
        session.flush()
        build_dataset_parquet(session, dataset, storage_root=path / "storage")
        session.commit()
        return document.id, dataset.id


def test_parquet_build_and_duckdb_pushdown(tmp_path: Path):
    _document_id, dataset_id = _seed_and_build(tmp_path)

    async def run():
        # Reuse the builder database through an async engine so the production
        # service boundary is exercised without mocking DuckDB.
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'builder.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            literals = await discover_dataset_literals(
                session,
                dataset_id,
                "7月31日华东的金额合计是多少？",
                storage_root=tmp_path / "storage",
            )
            result = await execute_dataset_query(
                session,
                dataset_id,
                {
                    "filters": [{"column": "地区", "operator": "eq", "value": "华东"}],
                    "group_by": ["地区"],
                    "metric": "sum",
                    "metric_column": "金额",
                    "sort_order": "desc",
                    "limit": 10,
                },
                storage_root=tmp_path / "storage",
            )
        await engine.dispose()
        return result, literals

    result, literals = asyncio.run(run())
    assert result["backend"] == "duckdb"
    assert result["matched_row_count"] == 2
    assert result["rows"] == [{"地区": "华东", "metric": 30.0, "matched_rows": 2}]
    assert ("日期", "2026-07-31") in literals
    assert ("地区", "华东") in literals
    assert (
        tmp_path / "storage" / "datasets" / str(dataset_id) / "v1.parquet"
    ).is_file()


def test_dataset_query_accepts_op_alias(tmp_path: Path):
    _document_id, dataset_id = _seed_and_build(tmp_path)

    async def run():
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'builder.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            result = await execute_dataset_query(
                session,
                dataset_id,
                {
                    "filters": [{"column": "地区", "op": "eq", "value": "华东"}],
                    "columns": ["地区", "金额"],
                    "limit": 10,
                },
                storage_root=tmp_path / "storage",
            )
        await engine.dispose()
        return result

    result = asyncio.run(run())
    assert result["matched_row_count"] == 2
    assert {row["地区"] for row in result["rows"]} == {"华东"}


def test_dataset_query_aggregates_only_direct_hierarchy_children(tmp_path: Path):
    _document_id, dataset_id = _seed_and_build(tmp_path)

    async def run():
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'builder.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            result = await execute_dataset_query(
                session,
                dataset_id,
                {
                    "filters": [
                        {
                            "column": "地区",
                            "operator": "direct_child_of",
                            "value": "全国",
                        }
                    ],
                    "metric": "sum",
                    "metric_column": "金额",
                    "limit": 10,
                },
                storage_root=tmp_path / "storage",
            )
        await engine.dispose()
        return result

    result = asyncio.run(run())
    assert result["matched_row_count"] == 2
    assert result["rows"] == [{"metric": 70.0, "matched_rows": 2}]


def test_zero_parent_query_returns_generic_hierarchy_recovery_hint(tmp_path: Path):
    _document_id, dataset_id = _seed_and_build(tmp_path)

    async def run():
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'builder.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            result = await execute_dataset_query(
                session,
                dataset_id,
                {
                    "filters": [{"column": "地区", "operator": "eq", "value": "全国"}],
                    "columns": ["地区", "金额"],
                    "limit": 10,
                },
                storage_root=tmp_path / "storage",
            )
        await engine.dispose()
        return result

    result = asyncio.run(run())
    assert result["matched_row_count"] == 0
    assert result["query_hints"] == [
        {
            "type": "hierarchy_children_available",
            "message": (
                "精确父级没有记录，但字段样例显示存在下级路径；"
                "可保留其他筛选条件，将该条件改为 direct_child_of 验证直属子级。"
            ),
            "suggested_filter": {
                "column": "地区",
                "operator": "direct_child_of",
                "value": "全国",
            },
        }
    ]


def test_query_plan_rejects_unknown_columns(tmp_path: Path):
    _document_id, dataset_id = _seed_and_build(tmp_path)

    async def run():
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'builder.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            with pytest.raises(DatasetExecutionError) as error:
                await execute_dataset_query(
                    session,
                    dataset_id,
                    {"columns": ["DROP TABLE documents"], "metric": "rows"},
                    storage_root=tmp_path / "storage",
                )
        await engine.dispose()
        return error.value

    assert asyncio.run(run()).code == "invalid_query"


def test_failed_rebuild_keeps_previous_active_artifact(tmp_path: Path, monkeypatch):
    _document_id, dataset_id = _seed_and_build(tmp_path)
    engine = create_engine(f"sqlite:///{tmp_path / 'builder.db'}")

    def fail_replace(_source, _target):
        raise OSError("disk unavailable")

    monkeypatch.setattr("apps.api.services.dataset_execution.os.replace", fail_replace)
    with Session(engine) as session:
        dataset = session.get(KnowledgeDataset, dataset_id)
        with pytest.raises(DatasetExecutionError):
            build_dataset_parquet(session, dataset, storage_root=tmp_path / "storage")
        session.rollback()
        active = session.scalar(
            select(DatasetArtifact).where(
                DatasetArtifact.dataset_id == dataset_id,
                DatasetArtifact.is_active.is_(True),
            )
        )
        assert active is not None
        assert active.version_number == 1
        assert active.status == "ready"
        assert (
            session.query(DatasetArtifact).filter_by(dataset_id=dataset_id).count() == 1
        )
    assert (
        tmp_path / "storage" / "datasets" / str(dataset_id) / "v1.parquet"
    ).is_file()
    assert not (
        tmp_path / "storage" / "datasets" / str(dataset_id) / "v2.parquet"
    ).exists()


def test_null_is_rejected_for_ordered_comparison(tmp_path: Path):
    _document_id, dataset_id = _seed_and_build(tmp_path)

    async def run():
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'builder.db'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            with pytest.raises(DatasetExecutionError) as error:
                await execute_dataset_query(
                    session,
                    dataset_id,
                    {
                        "filters": [
                            {"column": "金额", "operator": "gt", "value": None}
                        ],
                        "metric": "count",
                    },
                    storage_root=tmp_path / "storage",
                )
        await engine.dispose()
        return error.value

    assert asyncio.run(run()).code == "invalid_query"


def test_artifact_worker_retries_unexpected_error_without_failing_document(
    tmp_path: Path, monkeypatch
):
    document_id, _dataset_id = _seed_and_build(tmp_path)
    engine = create_engine(f"sqlite:///{tmp_path / 'builder.db'}")
    with Session(engine) as session:
        document = session.get(Document, document_id)
        version = session.get(DocumentVersion, document.current_version_id)
        job = ProcessingJob(
            document_id=document.id,
            document_version_id=version.id,
            stage="dataset_artifact",
            status="processing",
            idempotency_key="test:artifact:unexpected",
            retry_count=0,
            max_retries=3,
            config_version="dataset-parquet:v1",
        )
        session.add(job)
        session.commit()
        job_id = job.id
        version_id = version.id

        def fail_build(*_args, **_kwargs):
            raise RuntimeError("unexpected failure")

        monkeypatch.setattr(
            "apps.worker.services.processor.build_dataset_parquet", fail_build
        )
        assert _process_dataset_artifacts(session, job, version) is False
        session.expire_all()
        retried = session.get(ProcessingJob, job_id)
        current_version = session.get(DocumentVersion, version_id)
        assert retried.status == "retry"
        assert retried.error_details["code"] == "artifact_build_failed"
        assert current_version.processing_status == "ready"
