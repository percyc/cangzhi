"""Regression tests for pinned ``/documents/{id}/original`` version handling.

A WebDAV document whose source blob was released after parsing keeps only a
``content_hash`` on the version. Requesting that version explicitly must fetch
the remote original and only serve it when the downloaded bytes hash to the
recorded ``version.content_hash``; otherwise it must refuse with 409 rather
than silently impersonating old evidence with newer/different content. Local
blobs win when present, unpinned requests keep the existing on-demand WebDAV
semantics, and source/entry/workspace constraints are enforced.
"""

from __future__ import annotations

import asyncio
from io import BytesIO

from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.documents import Document, DocumentVersion
from apps.api.models.webdav import WebDAVEntry
from apps.api.models.workspaces import Workspace
from apps.api.services.webdav import WebDAVError

PDF_MIME = "application/pdf"


def _run(coro):
    return asyncio.run(coro)


async def _open_db():
    """Yield one request-scoped DB session from the app's dependency override."""
    dependency = app.dependency_overrides[get_db]()
    session = await anext(dependency)
    return session, dependency


def _create_source(test_client, *, name="云盘", workspace: str | None = None) -> int:
    url = "/api/webdav"
    if workspace:
        url += f"?workspace={workspace}"
    response = test_client.post(
        url,
        json={
            "name": name,
            "base_url": "https://dav.example.com/",
            "username": "reader",
            "password": "secret",
            "root_path": "/knowledge",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(test_client, content, *, filename="report.pdf", mime=PDF_MIME):
    response = test_client.post(
        "/api/files/upload",
        files={"file": (filename, BytesIO(content), mime)},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {
        "doc_id": body["id"],
        "version_id": body["current_version"]["id"],
        "content_hash": body["current_version"]["content_hash"],
    }


def _create_additional_workspace(slug):
    async def mutate():
        session, dependency = await _open_db()
        try:
            session.add(
                Workspace(
                    slug=slug,
                    name=slug,
                    is_default=False,
                    status="active",
                    settings={},
                )
            )
            await session.commit()
        finally:
            await dependency.aclose()

    _run(mutate())


def _seed_webdav_document(
    test_client,
    content,
    *,
    source_id: int | None = None,
    workspace: str | None = None,
    remote_path="/knowledge/report.pdf",
    filename="report.pdf",
    release=True,
    add_historical=True,
):
    """Create a WebDAV-marked document with an optional historical version.

    Returns a dict with ``doc_id``, ``v1_id`` (initial/current unless a
    historical version is added), ``v2_id``, content bytes/hash, source id and
    remote path. Defaults mirror the "source released after parsing" scenario
    (``release=True`` sets the version blob to None).
    """
    if source_id is None:
        source_id = _create_source(test_client, workspace=workspace)
    uploaded = _upload(test_client, content, filename=filename)
    doc_id, v1_id, content_hash = (
        uploaded["doc_id"],
        uploaded["version_id"],
        uploaded["content_hash"],
    )

    captured = {"v2_id": None}

    async def mutate():
        session, dependency = await _open_db()
        try:
            document = await session.get(Document, doc_id)
            document.meta = {
                "external_source": "webdav",
                "webdav_source_id": source_id,
                "webdav_path": remote_path,
            }
            session.add(
                WebDAVEntry(
                    source_id=source_id,
                    remote_path=remote_path,
                    document_id=doc_id,
                    state="synced",
                )
            )
            v1 = await session.get(DocumentVersion, v1_id)
            if release:
                v1.blob_id = None
            if add_historical:
                v2 = DocumentVersion(
                    document_id=doc_id,
                    version_number=2,
                    content_hash=content_hash,
                    processing_status="ready",
                )
                session.add(v2)
                await session.flush()
                if release:
                    v2.blob_id = None
                document.current_version_id = v2.id
                captured["v2_id"] = v2.id
            await session.commit()
        finally:
            await dependency.aclose()

    _run(mutate())
    return {
        "doc_id": doc_id,
        "v1_id": v1_id,
        "v2_id": captured["v2_id"],
        "content_hash": content_hash,
        "content": content,
        "source_id": source_id,
        "remote_path": remote_path,
    }


def _point_meta_at_other_source(test_client, doc_id, source_id):
    async def mutate():
        session, dependency = await _open_db()
        try:
            document = await session.get(Document, doc_id)
            document.meta = {**document.meta, "webdav_source_id": source_id}
            await session.commit()
        finally:
            await dependency.aclose()

    _run(mutate())


def _release_blob(test_client, doc_id, version_id):
    async def mutate():
        session, dependency = await _open_db()
        try:
            version = await session.get(DocumentVersion, version_id)
            version.blob_id = None
            await session.commit()
        finally:
            await dependency.aclose()

    _run(mutate())


def _add_entry(test_client, doc_id, source_id, remote_path):
    async def mutate():
        session, dependency = await _open_db()
        try:
            session.add(
                WebDAVEntry(
                    source_id=source_id,
                    remote_path=remote_path,
                    document_id=doc_id,
                    state="synced",
                )
            )
            await session.commit()
        finally:
            await dependency.aclose()

    _run(mutate())


def _delete_entry(test_client, doc_id):
    async def mutate():
        session, dependency = await _open_db()
        try:
            await session.execute(
                WebDAVEntry.__table__.delete().where(
                    WebDAVEntry.document_id == doc_id
                )
            )
            await session.commit()
        finally:
            await dependency.aclose()

    _run(mutate())


# ---------------------------------------------------------------------------
# Pinned versions
# ---------------------------------------------------------------------------


def test_explicit_webdav_version_matching_hash_served(client, monkeypatch):
    test_client, _ = client
    seeded = _seed_webdav_document(
        test_client, b"# report\nrecorded evidence", add_historical=False
    )
    download_calls = {"n": 0}

    async def fake_download(**_kwargs):
        download_calls["n"] += 1
        return seeded["content"], PDF_MIME

    monkeypatch.setattr("apps.api.api.documents.download_file", fake_download)

    response = test_client.get(
        f"/api/documents/{seeded['doc_id']}/original"
        f"?version_id={seeded['v1_id']}"
    )
    assert response.status_code == 200
    assert response.content == seeded["content"]
    assert response.headers["content-type"].startswith("application/pdf")
    assert download_calls["n"] == 1


def test_explicit_webdav_current_and_historical_same_hash_served(
    client, monkeypatch
):
    test_client, _ = client
    content = b"# shared evidence\nimmutable bytes"
    seeded = _seed_webdav_document(test_client, content, add_historical=True)

    async def fake_download(**_kwargs):
        return content, PDF_MIME

    monkeypatch.setattr("apps.api.api.documents.download_file", fake_download)

    # historical v1 + current v2 share the same recorded hash; both succeed.
    for version_id in (seeded["v1_id"], seeded["v2_id"]):
        response = test_client.get(
            f"/api/documents/{seeded['doc_id']}/original"
            f"?version_id={version_id}"
        )
        assert response.status_code == 200, version_id
        assert response.content == content
    assert seeded["v2_id"] is not None


def test_explicit_webdav_version_mismatched_hash_rejected(client, monkeypatch):
    test_client, _ = client
    seeded = _seed_webdav_document(
        test_client, b"# recorded\noriginal snapshot", add_historical=False
    )

    async def fake_download(**_kwargs):
        # Remote changed: bytes no longer match the pinned version's hash.
        return b"# different\nnewer content", PDF_MIME

    monkeypatch.setattr("apps.api.api.documents.download_file", fake_download)

    response = test_client.get(
        f"/api/documents/{seeded['doc_id']}/original"
        f"?version_id={seeded['v1_id']}"
    )
    assert response.status_code == 409
    assert "变化" in response.json()["detail"]


def test_unpinned_webdav_keeps_on_demand_semantics(client, monkeypatch):
    """No pinned version keeps the legacy on-demand fetch (no hash check)."""
    test_client, _ = client
    seeded = _seed_webdav_document(
        test_client, b"# recorded\nold hash", add_historical=False
    )
    remote_bytes = b"# whatever\ncurrent remote"

    async def fake_download(**_kwargs):
        return remote_bytes, PDF_MIME

    monkeypatch.setattr("apps.api.api.documents.download_file", fake_download)

    response = test_client.get(f"/api/documents/{seeded['doc_id']}/original")
    assert response.status_code == 200
    assert response.content == remote_bytes


# ---------------------------------------------------------------------------
# Local blob preference
# ---------------------------------------------------------------------------


def test_explicit_webdav_version_prefers_local_blob(client, monkeypatch):
    test_client, _ = client
    # Keep the blob (release=False): the explicit request must serve the local
    # original and never hit the remote.
    seeded = _seed_webdav_document(
        test_client, b"# local\nkept source", release=False, add_historical=False
    )
    download_calls = {"n": 0}

    async def fail_if_called(**_kwargs):
        download_calls["n"] += 1
        raise AssertionError("remote must not be fetched when local blob exists")

    monkeypatch.setattr("apps.api.api.documents.download_file", fail_if_called)

    response = test_client.get(
        f"/api/documents/{seeded['doc_id']}/original"
        f"?version_id={seeded['v1_id']}"
    )
    assert response.status_code == 200
    assert response.content == seeded["content"]
    assert download_calls["n"] == 0


def test_plain_upload_explicit_version_serves_local_blob(client):
    test_client, _ = client
    content = b"plain uploaded original bytes"
    seeded = _upload(test_client, content, filename="note.md", mime="text/markdown")

    response = test_client.get(
        f"/api/documents/{seeded['doc_id']}/original"
        f"?version_id={seeded['version_id']}"
    )
    assert response.status_code == 200
    assert response.content == content
    assert "note.md" in response.headers["content-disposition"]


# ---------------------------------------------------------------------------
# Source / entry / workspace constraints
# ---------------------------------------------------------------------------


def test_explicit_webdav_missing_connector_409(client, monkeypatch):
    test_client, _ = client
    seeded = _seed_webdav_document(
        test_client, b"# orphan\nblob released", add_historical=False
    )
    # Point the document at a connector that no longer exists.
    _point_meta_at_other_source(test_client, seeded["doc_id"], 999_999)
    async def fake_download(**_kwargs):
        return seeded["content"], PDF_MIME

    monkeypatch.setattr("apps.api.api.documents.download_file", fake_download)

    response = test_client.get(
        f"/api/documents/{seeded['doc_id']}/original"
        f"?version_id={seeded['v1_id']}"
    )
    assert response.status_code == 409
    assert "连接器" in response.json()["detail"]


def test_explicit_webdav_missing_entry_409(client, monkeypatch):
    test_client, _ = client
    seeded = _seed_webdav_document(
        test_client, b"# no entry\nblob released", add_historical=False
    )
    _delete_entry(test_client, seeded["doc_id"])

    async def fake_download(**_kwargs):
        return seeded["content"], PDF_MIME

    monkeypatch.setattr("apps.api.api.documents.download_file", fake_download)

    response = test_client.get(
        f"/api/documents/{seeded['doc_id']}/original"
        f"?version_id={seeded['v1_id']}"
    )
    assert response.status_code == 409
    assert "连接器" in response.json()["detail"]


def test_explicit_webdav_download_failure_502(client, monkeypatch):
    test_client, _ = client
    seeded = _seed_webdav_document(
        test_client, b"# failed\nblob released", add_historical=False
    )

    async def failing_download(**_kwargs):
        raise WebDAVError("https://reader:secret@example.invalid/sensitive-upstream-diagnostic")

    monkeypatch.setattr("apps.api.api.documents.download_file", failing_download)

    response = test_client.get(
        f"/api/documents/{seeded['doc_id']}/original"
        f"?version_id={seeded['v1_id']}"
    )
    assert response.status_code == 502
    assert "获取 WebDAV 原文件失败" in response.json()["detail"]
    assert "secret" not in response.text
    assert "sensitive-upstream-diagnostic" not in response.text


def test_explicit_webdav_source_in_other_workspace_rejected(client, monkeypatch):
    test_client, _ = client
    # A second workspace owns a connector; the (default-workspace) document
    # must not be allowed to fetch through it.
    _create_additional_workspace("team")
    foreign_source = _create_source(test_client, name="团队盘", workspace="team")
    seeded = _seed_webdav_document(
        test_client,
        b"# cross workspace\nblob released",
        source_id=foreign_source,
        add_historical=False,
    )

    async def fake_download(**_kwargs):
        return seeded["content"], PDF_MIME

    monkeypatch.setattr("apps.api.api.documents.download_file", fake_download)

    response = test_client.get(
        f"/api/documents/{seeded['doc_id']}/original"
        f"?version_id={seeded['v1_id']}"
    )
    assert response.status_code == 409
    assert "连接器" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Disposition
# ---------------------------------------------------------------------------


def test_explicit_webdav_inline_and_attachment_disposition(client, monkeypatch):
    test_client, _ = client
    seeded = _seed_webdav_document(
        test_client, b"# disposition\ninline vs attachment", add_historical=False
    )

    async def fake_download(**_kwargs):
        return seeded["content"], PDF_MIME

    monkeypatch.setattr("apps.api.api.documents.download_file", fake_download)

    inline = test_client.get(
        f"/api/documents/{seeded['doc_id']}/original"
        f"?inline=true&version_id={seeded['v1_id']}"
    )
    assert inline.status_code == 200
    assert inline.headers["content-disposition"].startswith("inline;")

    attachment = test_client.get(
        f"/api/documents/{seeded['doc_id']}/original"
        f"?version_id={seeded['v1_id']}"
    )
    assert attachment.status_code == 200
    assert attachment.headers["content-disposition"].startswith("attachment;")
