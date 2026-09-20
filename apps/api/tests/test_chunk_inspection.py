import asyncio

from sqlalchemy import delete

from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.chunks import DocumentChunk
from apps.api.models.documents import DocumentVersion


def test_current_chunk_preview_is_paginated_and_bounded(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes", json={"title": "切片预览", "content": "事实原文"}
    ).json()
    document_id = created["id"]
    version_id = created["current_version"]["id"]
    dependency_factory = app.dependency_overrides[get_db]

    async def seed():
        dependency = dependency_factory()
        session = await anext(dependency)
        try:
            version = await session.get(DocumentVersion, version_id)
            await session.execute(
                delete(DocumentChunk).where(
                    DocumentChunk.document_version_id == version_id
                )
            )
            version.meta = {
                **(version.meta or {}),
                "chunk_quality": {
                    "quality_level": "good",
                    "quality_score": 96,
                    "model_calls": 0,
                },
            }
            for index in range(3):
                session.add(
                    DocumentChunk(
                        document_id=document_id,
                        document_version_id=version_id,
                        external_id=f"preview-{index}",
                        role="child",
                        chunk_type="paragraph",
                        order_index=index,
                        content=(f"片段 {index} " + "甲" * 2_500),
                        search_text=f"片段 {index}",
                        content_hash=str(index) * 64,
                        heading_path=["章节"],
                        page=index + 1,
                        char_count=2_505,
                        token_estimate=1_000,
                        is_current=True,
                    )
                )
            await session.commit()
        finally:
            await dependency.aclose()

    asyncio.run(seed())

    response = test_client.get(
        f"/api/documents/{document_id}/chunks-preview?offset=1&limit=1"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["child_total"] == 3
    assert payload["offset"] == 1
    assert len(payload["items"]) == 1
    assert payload["items"][0]["order_index"] == 1
    assert payload["items"][0]["truncated"] is True
    assert len(payload["items"][0]["content"]) == 2_400
    assert payload["quality"]["quality_score"] == 96
