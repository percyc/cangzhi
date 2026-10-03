import asyncio

from sqlalchemy import event

from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.documents import Document, DocumentSourceType, DocumentVersion
from apps.api.models.workspaces import Workspace
from apps.api.api.schemas import DocumentPipelineResponse
from apps.api.services.inbox import FILTERS
from test_processing_status import _seed_base


def test_inbox_pagination_counts_and_workspace_boundary(client):
    test_client, _ = client

    async def seed():
        async for db in app.dependency_overrides[get_db]():
            workspace = Workspace(slug="environment", name="环评", settings={})
            db.add(workspace)
            await db.flush()
            for index in range(104):
                document = Document(
                    title=f"report-{index}",
                    source_type=DocumentSourceType.file,
                    workspace_id=workspace.id,
                    is_deleted=index == 103,
                )
                db.add(document)
                await db.flush()
                version = DocumentVersion(
                    document_id=document.id,
                    version_number=1,
                    content_hash=f"hash-{index}",
                    raw_content="body",
                    processing_status="failed" if index < 3 else "ready",
                )
                db.add(version)
                await db.flush()
                document.current_version_id = version.id
            await db.commit()

    asyncio.run(seed())
    headers = {"X-Cangzhi-Workspace": "environment"}
    first = test_client.get("/api/documents/inbox?filter=all&limit=25", headers=headers)
    assert first.status_code == 200
    payload = first.json()
    assert payload["total"] == payload["counts"]["all"] == 103
    assert len(payload["items"]) == 25
    assert payload["counts"]["failed"] == 3
    assert payload["has_processing"]
    ids = []
    for offset in range(0, 103, 25):
        page = test_client.get(
            f"/api/documents/inbox?filter=all&offset={offset}&limit=25", headers=headers
        ).json()
        ids.extend(item["id"] for item in page["items"])
    assert len(ids) == len(set(ids)) == 103
    failed = test_client.get(
        "/api/documents/inbox?filter=failed", headers=headers
    ).json()
    assert failed["total"] == len(failed["items"]) == 3
    assert all(
        item["pipeline"]["overall_status"] == "failed" for item in failed["items"]
    )
    assert test_client.get("/api/documents/inbox").json()["total"] == 0
    assert (
        test_client.get(
            "/api/documents/inbox?filter=all&offset=200", headers=headers
        ).json()["items"]
        == []
    )
    for query in ("limit=0", "limit=101", "offset=-1", "filter=unknown"):
        assert test_client.get(f"/api/documents/inbox?{query}").status_code == 422


def test_inbox_filter_semantics_match_overview_and_no_heavy_columns(client):
    test_client, _ = client
    created = test_client.post(
        "/api/notes", json={"title": "note", "content": "body"}
    ).json()
    version_id = created["current_version"]["id"]
    captured = []
    engines = []

    def capture(_conn, _cursor, _statement, _params, context, _many):
        # Inspect the actual returned columns, including ORM load_only projection.
        if _cursor.description:
            captured.extend(column[0] for column in _cursor.description)

    async def seed():
        async for db in app.dependency_overrides[get_db]():
            await _seed_base(db, created["id"], version_id)
            doc = await db.get(Document, created["id"])
            doc.meta = {"external_source": "webdav", "webdav_source_id": 987654}
            version = await db.get(DocumentVersion, version_id)
            version.structured_content = {"blocks": [{"text": "huge" * 10000}]}
            await db.commit()
            engine = db.bind.sync_engine
            engines.append(engine)
            event.listen(engine, "after_cursor_execute", capture)

    asyncio.run(seed())
    try:
        overview = test_client.get(
            "/api/documents/overview?include_processing=true"
        ).json()[0]
        for filter in FILTERS:
            response = test_client.get(f"/api/documents/inbox?filter={filter}")
            assert response.status_code == 200
            payload = response.json()
            assert payload["total"] == payload["counts"][filter]
            assert len(payload["items"]) == payload["total"]
            if payload["items"]:
                item = payload["items"][0]
                assert (
                    DocumentPipelineResponse.model_validate(
                        item["pipeline"]
                    ).model_dump()
                    == overview["pipeline"]
                )
                assert item["primary_category"] == overview["primary_category"]
        assert (
            test_client.get("/api/documents/inbox?filter=source_issue").json()["total"]
            == 1
        )
    finally:
        for engine in engines:
            event.remove(engine, "after_cursor_execute", capture)
    forbidden = {
        "vector",
        "content",
        "search_text",
        "raw_content",
        "structured_content",
    }
    assert captured
    assert not set(captured) & forbidden


def test_inbox_requires_authentication(client):
    from apps.api.api.auth import require_admin

    test_client, _ = client
    app.dependency_overrides.pop(require_admin)
    assert test_client.get("/api/documents/inbox").status_code == 401


def test_optional_disabled_vector_does_not_create_attention(client):
    from sqlalchemy import select
    from apps.api.models.auth import AIRuntimeConfig

    http, _ = client
    created = http.post("/api/notes", json={"title": "Synthetic organized", "content": "Synthetic facts"}).json()
    version_id = created["current_version"]["id"]
    async def prepare():
        async for db in app.dependency_overrides[get_db]():
            await _seed_base(db, created["id"], version_id)
            config = await db.scalar(select(AIRuntimeConfig))
            config.active_embedding_profile_id = None
            await db.commit()
    asyncio.run(prepare())
    payload = http.get("/api/documents/inbox?filter=all").json()
    assert payload["items"][0]["pipeline"]["stages"]["embedding"]["status"] == "disabled"
    assert payload["items"][0]["pipeline"]["keyword_searchable"] is True
    assert payload["counts"]["completed"] == 1
    assert payload["counts"]["attention"] == payload["counts"]["not_vectorized"] == 0
    assert http.get("/api/documents/inbox?filter=attention").json()["items"] == []


def test_database_snapshot_error_is_a_source_issue_without_hiding_available_data(client):
    from apps.api.models.database_source import DatabaseSnapshot, DatabaseSource
    from apps.api.models.datasets import KnowledgeDataset

    http, _ = client
    created = http.post("/api/notes", json={"title": "Synthetic snapshot", "content": "Synthetic facts"}).json()
    version_id = created["current_version"]["id"]
    async def prepare():
        async for db in app.dependency_overrides[get_db]():
            await _seed_base(db, created["id"], version_id)
            source = DatabaseSource(name="Synthetic source", engine="mysql", host="example.invalid", port=3306,
                database_name="synthetic", username="readonly", password_cipher="synthetic-not-a-key", status="error")
            dataset = KnowledgeDataset(document_id=created["id"], document_version_id=version_id,
                name="Synthetic table", sheet_name="synthetic", region_index=0)
            db.add_all([source, dataset])
            await db.flush()
            db.add(DatabaseSnapshot(source_id=source.id, document_id=created["id"], dataset_id=dataset.id,
                schema_name="synthetic", table_name="facts", last_error="Synthetic refresh failure"))
            healthy = Document(title="Synthetic healthy sibling", source_type=DocumentSourceType.file)
            db.add(healthy)
            await db.flush()
            healthy_version = DocumentVersion(document_id=healthy.id, version_number=1,
                content_hash="healthy", raw_content="Synthetic healthy facts", processing_status="ready")
            db.add(healthy_version)
            await db.flush()
            healthy.current_version_id = healthy_version.id
            healthy_dataset = KnowledgeDataset(document_id=healthy.id, document_version_id=healthy_version.id,
                name="Synthetic healthy table", sheet_name="healthy", region_index=0)
            db.add(healthy_dataset)
            await db.flush()
            db.add(DatabaseSnapshot(source_id=source.id, document_id=healthy.id, dataset_id=healthy_dataset.id,
                schema_name="synthetic", table_name="healthy"))
            db.add(Workspace(slug="isolated", name="Synthetic empty space", settings={}))
            await db.commit()
    asyncio.run(prepare())
    payload = http.get("/api/documents/inbox?filter=source_issue").json()
    assert payload["total"] == 1
    item = payload["items"][0]
    assert item["origin"] == {"kind": "database", "label": "Synthetic source", "connector_available": True, "source_status": "failed"}
    assert item["pipeline"]["keyword_searchable"] is True
    assert http.get("/api/documents/inbox?filter=source_issue", headers={"X-Cangzhi-Workspace": "isolated"}).json()["total"] == 0


def test_governance_only_table_remains_a_real_todo_without_vector_repair(client):
    http, _ = client
    created = http.post("/api/notes", json={"title": "Synthetic governance", "content": "Synthetic facts"}).json()
    version_id = created["current_version"]["id"]
    async def prepare():
        async for db in app.dependency_overrides[get_db]():
            await _seed_base(db, created["id"], version_id)
            version = await db.get(DocumentVersion, version_id)
            version.meta = {"spreadsheet_processing": {"mode": "governance", "governance_required": True}}
            await db.commit()
    asyncio.run(prepare())
    payload = http.get("/api/documents/inbox?filter=attention").json()
    assert payload["total"] == payload["counts"]["needs_organization"] == 1
    assert payload["counts"]["not_vectorized"] == 0
    assert payload["items"][0]["pipeline"]["stages"]["embedding"]["status"] == "skipped"
