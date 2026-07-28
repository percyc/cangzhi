from sqlalchemy import select

from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.processing import ProcessingJob
from apps.api.models.taxonomy import Category, DocumentCategory


def test_update_document_category_success(client):
    test_client, _ = client
    category = test_client.post(
        "/api/categories",
        json={"slug": "manual-tech", "name": "技术"},
    ).json()
    created = test_client.post(
        "/api/notes",
        json={"title": "手动分类", "content": "测试手动分类选择"},
    ).json()
    document_id = created["id"]
    version_id = created["current_version"]["id"]

    response = test_client.patch(
        f"/api/documents/{document_id}/category",
        json={"category_id": category["id"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["primary_category"]["id"] == category["id"]
    assert body["primary_category"]["name"] == "技术"
    assert [c["id"] for c in body["categories"]] == [category["id"]]

    db_dependency = app.dependency_overrides[get_db]
    import asyncio

    async def fetch_links():
        dependency = db_dependency()
        session = await anext(dependency)
        try:
            result = await session.execute(
                select(DocumentCategory).where(
                    DocumentCategory.document_version_id == version_id
                )
            )
            return list(result.scalars().all())
        finally:
            await dependency.aclose()

    links = asyncio.run(fetch_links())
    assert len(links) == 1
    assert links[0].is_primary is True
    assert links[0].source == "user"
    assert links[0].confidence == 1.0
    assert links[0].category_id == category["id"]


def test_update_document_category_replaces_existing(client):
    test_client, _ = client
    first = test_client.post(
        "/api/categories",
        json={"slug": "manual-old", "name": "旧分类"},
    ).json()
    second = test_client.post(
        "/api/categories",
        json={"slug": "manual-new", "name": "新分类"},
    ).json()
    created = test_client.post(
        "/api/notes",
        json={"title": "替换分类", "content": "先选一个再换"},
    ).json()
    document_id = created["id"]

    test_client.patch(
        f"/api/documents/{document_id}/category",
        json={"category_id": first["id"]},
    )
    response = test_client.patch(
        f"/api/documents/{document_id}/category",
        json={"category_id": second["id"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["primary_category"]["id"] == second["id"]
    assert [c["id"] for c in body["categories"]] == [second["id"]]


def test_update_document_category_unknown_document(client):
    test_client, _ = client
    response = test_client.patch(
        "/api/documents/9999/category",
        json={"category_id": 1},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "资料不存在"


def test_update_document_category_unknown_category(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes",
        json={"title": "未知分类", "content": "选不到分类"},
    ).json()
    response = test_client.patch(
        f"/api/documents/{created['id']}/category",
        json={"category_id": 9999},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "分类不存在"


def test_saving_model_queues_unclassified_documents(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes",
        json={"title": "等待自动分类", "content": "这条资料需要模型自动整理"},
    ).json()
    version_id = created["current_version"]["id"]

    response = test_client.patch(
        "/api/settings/ai",
        json={
            "provider": "openai",
            "openai": {
                "base_url": "https://api.example.com/v1",
                "model": "chat-model",
            },
            "api_key_action": "replace",
            "api_key": "sk-test-auto-organize",
        },
    )
    assert response.status_code == 200

    db_dependency = app.dependency_overrides[get_db]
    import asyncio

    async def fetch_job():
        dependency = db_dependency()
        session = await anext(dependency)
        try:
            return await session.scalar(
                select(ProcessingJob).where(
                    ProcessingJob.document_version_id == version_id,
                    ProcessingJob.stage == "understanding",
                )
            )
        finally:
            await dependency.aclose()

    job = asyncio.run(fetch_job())
    assert job is not None
    assert job.status == "created"
