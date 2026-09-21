from __future__ import annotations

import asyncio

from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.datasets import DatasetField, KnowledgeDataset
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.table_rows import StructuredTableRow


def _seed_dataset() -> tuple[int, int]:
    async def seed() -> tuple[int, int]:
        generator = app.dependency_overrides[get_db]()
        session = await anext(generator)
        try:
            document = Document(title="人员清单", source_type=DocumentSourceType.file)
            session.add(document)
            await session.flush()
            version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                content_hash="dataset-api",
                processing_status="ready",
            )
            session.add(version)
            await session.flush()
            document.current_version_id = version.id
            dataset = KnowledgeDataset(
                document_id=document.id,
                document_version_id=version.id,
                name="人员",
                sheet_name="人员",
                region_index=1,
                dataset_kind="table",
                status="ready",
                row_count=2,
                column_count=2,
                source_row_start=2,
                source_row_end=3,
                profile={"quality": {"completeness": 0.75}},
            )
            session.add(dataset)
            await session.flush()
            session.add_all(
                [
                    DatasetField(
                        dataset_id=dataset.id,
                        position=0,
                        name="姓名",
                        inferred_type="text",
                        null_count=0,
                        distinct_count=2,
                        sample_values=["甲", "乙"],
                        statistics={},
                    ),
                    DatasetField(
                        dataset_id=dataset.id,
                        position=1,
                        name="年龄",
                        inferred_type="number",
                        null_count=1,
                        distinct_count=1,
                        sample_values=["20"],
                        statistics={"min": 20, "max": 20},
                    ),
                    StructuredTableRow(
                        document_id=document.id,
                        document_version_id=version.id,
                        dataset_id=dataset.id,
                        sheet_name="人员",
                        region_index=1,
                        row_number=2,
                        values={"姓名": "甲", "年龄": "20"},
                    ),
                    StructuredTableRow(
                        document_id=document.id,
                        document_version_id=version.id,
                        dataset_id=dataset.id,
                        sheet_name="人员",
                        region_index=1,
                        row_number=3,
                        values={"姓名": "乙", "年龄": None},
                    ),
                ]
            )
            await session.commit()
            return document.id, dataset.id
        finally:
            await generator.aclose()

    return asyncio.run(seed())


def test_dataset_catalog_and_paginated_preview(client):
    test_client, _storage = client
    document_id, dataset_id = _seed_dataset()

    document = test_client.get(f"/api/documents/{document_id}")
    catalog = test_client.get(f"/api/datasets?document_id={document_id}")
    summary = test_client.get("/api/datasets/summary")
    preview = test_client.get(f"/api/datasets/{dataset_id}/rows?offset=1&limit=1")

    assert document.status_code == 200
    assert document.json()["content_kind"] == "dataset"
    assert catalog.status_code == 200
    assert summary.status_code == 200
    assert summary.json() == {
        "dataset_count": 1,
        "document_count": 1,
        "ready_count": 0,
        "examples": [{"id": dataset_id, "name": "人员", "sheet_name": "人员"}],
    }
    assert catalog.json()[0]["profile"]["quality"]["completeness"] == 0.75
    assert [field["name"] for field in catalog.json()[0]["fields"]] == [
        "姓名",
        "年龄",
    ]
    assert preview.status_code == 200
    assert preview.json()["total"] == 2
    assert preview.json()["rows"] == [
        {"row_number": 3, "姓名": "乙", "年龄": None}
    ]
