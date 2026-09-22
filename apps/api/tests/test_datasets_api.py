from __future__ import annotations

import asyncio
from types import SimpleNamespace

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


def test_generate_field_semantics_keeps_profile_facts(client, monkeypatch):
    test_client, _storage = client
    document_id, dataset_id = _seed_dataset()

    provider = SimpleNamespace(
        name="fake",
        _model="semantic-test",
        is_configured=lambda: True,
        generate_json=lambda **_kwargs: {
            "fields": [
                {
                    "name": "姓名",
                    "description": "人员姓名",
                    "unit": None,
                    "aliases": ["名字"],
                    "confidence": 0.9,
                },
                {
                    "name": "年龄",
                    "description": "人员年龄",
                    "unit": "岁",
                    "aliases": [],
                    "confidence": 0.8,
                },
            ]
        },
    )

    async def configured(_db):
        return provider

    monkeypatch.setattr("apps.api.api.datasets.build_provider_from_db", configured)
    response = test_client.post(f"/api/datasets/{dataset_id}/field-semantics")
    catalog = test_client.get(f"/api/datasets?document_id={document_id}").json()[0]

    assert response.status_code == 200
    assert response.json()["updated_fields"] == 2
    fields = {field["name"]: field for field in catalog["fields"]}
    assert fields["姓名"]["description"] == "人员姓名"
    assert fields["姓名"]["aliases"] == ["名字"]
    assert fields["年龄"]["unit"] == "岁"
    assert fields["年龄"]["inferred_type"] == "number"
    assert fields["年龄"]["statistics"] == {"min": 20, "max": 20}
