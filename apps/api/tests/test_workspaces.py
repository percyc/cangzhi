"""Focused tests for the workspace model, service, and API."""

import asyncio

from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.workspaces import (
    DEFAULT_WORKSPACE_SLUG,
    Workspace,
)


def _seed_workspace(
    client_fixture,
    *,
    slug,
    name="工作区",
    is_default=False,
    status="active",
):
    async def _insert():
        async for db in app.dependency_overrides[get_db]():
            ws = Workspace(
                slug=slug,
                name=name,
                is_default=is_default,
                status=status,
                settings={},
            )
            db.add(ws)
            await db.commit()
            await db.refresh(ws)
            return ws.id

    return asyncio.run(_insert())


def test_workspace_list_and_current(client):
    test_client, _ = client
    _seed_workspace(client, slug="research", name="研究")

    listing = test_client.get("/api/workspaces")
    assert listing.status_code == 200
    slugs = {item["slug"] for item in listing.json()}
    assert slugs == {DEFAULT_WORKSPACE_SLUG, "research"}

    current = test_client.get(
        "/api/workspaces/current", headers={"X-Cangzhi-Workspace": "research"}
    )
    assert current.status_code == 200
    assert current.json()["slug"] == "research"


def test_workspace_current_precedence_header_over_query_over_cookie(client):
    test_client, _ = client
    _seed_workspace(client, slug="research")
    _seed_workspace(client, slug="other")

    # Header wins over query and cookie.
    current = test_client.get(
        "/api/workspaces/current?workspace=other",
        headers={"X-Cangzhi-Workspace": "research"},
        cookies={"cangzhi_workspace": "other"},
    )
    assert current.status_code == 200
    assert current.json()["slug"] == "research"

    # Query wins over cookie.
    current = test_client.get(
        "/api/workspaces/current?workspace=research",
        cookies={"cangzhi_workspace": "other"},
    )
    assert current.status_code == 200
    assert current.json()["slug"] == "research"

    # Cookie wins over the default.
    current = test_client.get(
        "/api/workspaces/current", cookies={"cangzhi_workspace": "other"}
    )
    assert current.status_code == 200
    assert current.json()["slug"] == "other"

    # No selector falls back to the default.
    current = test_client.get("/api/workspaces/current")
    assert current.status_code == 200
    assert current.json()["slug"] == DEFAULT_WORKSPACE_SLUG


def test_workspace_current_unknown_404_and_archived_409(client):
    test_client, _ = client
    _seed_workspace(client, slug="old", status="archived")

    missing = test_client.get(
        "/api/workspaces/current", headers={"X-Cangzhi-Workspace": "nope"}
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "workspace_not_found"

    archived = test_client.get(
        "/api/workspaces/current", headers={"X-Cangzhi-Workspace": "old"}
    )
    assert archived.status_code == 409
    assert archived.json()["detail"]["code"] == "workspace_archived"


def test_workspace_create_update_get(client):
    test_client, _ = client
    created = test_client.post(
        "/api/workspaces",
        json={"slug": "research", "name": "研究", "description": "研究资料"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["slug"] == "research"
    assert body["status"] == "active"
    assert body["is_default"] is False

    duplicate = test_client.post(
        "/api/workspaces", json={"slug": "research", "name": "重复"}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "slug_exists"

    invalid = test_client.post(
        "/api/workspaces", json={"slug": "Bad Slug", "name": "非法"}
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "invalid_slug"

    fetched = test_client.get("/api/workspaces/research")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "研究"

    updated = test_client.patch(
        "/api/workspaces/research",
        json={"name": "研究重命名", "settings": {"theme": "dark"}},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "研究重命名"
    assert updated.json()["settings"] == {"theme": "dark"}


def test_workspace_archive_default_forbidden_and_other_archived(client):
    test_client, _ = client
    _seed_workspace(client, slug="research")

    blocked = test_client.post(f"/api/workspaces/{DEFAULT_WORKSPACE_SLUG}/archive")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "cannot_archive_default"

    archived = test_client.post("/api/workspaces/research/archive")
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"

    # Archiving flips the current-workspace selector for that slug.
    current = test_client.get(
        "/api/workspaces/current", headers={"X-Cangzhi-Workspace": "research"}
    )
    assert current.status_code == 409
    assert current.json()["detail"]["code"] == "workspace_archived"

    restored = test_client.post("/api/workspaces/research/restore")
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"

    current = test_client.get(
        "/api/workspaces/current", headers={"X-Cangzhi-Workspace": "research"}
    )
    assert current.status_code == 200
    assert current.json()["slug"] == "research"


def test_workspace_get_missing_404(client):
    test_client, _ = client
    missing = test_client.get("/api/workspaces/ghost")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "workspace_not_found"


def test_documents_and_categories_are_isolated_by_workspace(client):
    test_client, _ = client
    created_workspace = test_client.post(
        "/api/workspaces", json={"slug": "research", "name": "研究"}
    )
    assert created_workspace.status_code == 201

    default_note = test_client.post(
        "/api/notes", json={"title": "默认资料", "content": "默认空间正文"}
    ).json()
    research_headers = {"X-Cangzhi-Workspace": "research"}
    research_note = test_client.post(
        "/api/notes",
        headers=research_headers,
        json={"title": "研究资料", "content": "研究空间正文"},
    ).json()

    default_documents = test_client.get("/api/documents/overview").json()
    research_documents = test_client.get(
        "/api/documents/overview", headers=research_headers
    ).json()
    assert [item["id"] for item in default_documents] == [default_note["id"]]
    assert [item["id"] for item in research_documents] == [research_note["id"]]

    cross_space = test_client.get(
        f"/api/documents/{default_note['id']}", headers=research_headers
    )
    assert cross_space.status_code == 404

    default_categories = test_client.get("/api/categories").json()
    research_categories = test_client.get(
        "/api/categories", headers=research_headers
    ).json()
    assert default_categories == []
    assert len(research_categories) == 10
