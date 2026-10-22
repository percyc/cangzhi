"""Regression for version-pinned PDF previews after temporary WebDAV cleanup."""
import asyncio
from hashlib import sha256

import pytest

from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.documents import Document, DocumentVersion
from apps.api.models.webdav import WebDAVEntry


@pytest.mark.parametrize("same_bytes", [True, False])
def test_pinned_remote_pdf_requires_saved_version_bytes(client, monkeypatch, same_bytes):
    browser, storage = client
    original = b"%PDF-1.7\nsynthetic original\n%%EOF"
    source = browser.post("/api/webdav", json={
        "name": "test source", "base_url": "https://dav.example.com/",
        "username": "synthetic", "password": "test-only", "root_path": "/knowledge",
    })
    assert source.status_code == 201
    uploaded = browser.post("/api/files/upload", files={
        "file": ("manual.pdf", original, "application/pdf")})
    assert uploaded.status_code == 201
    doc_id = uploaded.json()["id"]
    version_id = uploaded.json()["current_version"]["id"]
    async def attach_remote():
        async for db in app.dependency_overrides[get_db]():
            doc = await db.get(Document, doc_id)
            doc.meta = {"external_source": "webdav", "webdav_source_id": source.json()["id"]}
            version = await db.get(DocumentVersion, version_id)
            assert version.content_hash == sha256(original).hexdigest()
            version.blob_id = None
            version.raw_content = "durable parsed text"
            version.processing_status = "ready"
            db.add(WebDAVEntry(source_id=source.json()["id"], document_id=doc_id,
                               remote_path="/knowledge/manual.pdf", state="synced"))
            await db.commit()
    asyncio.run(attach_remote())
    before = sorted(p.name for p in storage._blob_path.iterdir())
    calls = []
    async def remote(**kwargs):
        calls.append(kwargs["remote_path"])
        return (original if same_bytes else b"%PDF-1.7\nchanged"), "application/pdf"
    monkeypatch.setattr("apps.api.api.documents.download_file", remote)
    response = browser.get(
        f"/api/documents/{doc_id}/original?inline=true&version_id={version_id}&revision=undefined")
    assert calls == ["/knowledge/manual.pdf"]
    if same_bytes:
        assert response.status_code == 200
        assert response.content == original
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"].startswith("inline;")
        assert int(response.headers["content-length"]) == len(original)
    else:
        assert response.status_code == 409
        assert "已变化" in response.json()["detail"]
    assert sorted(p.name for p in storage._blob_path.iterdir()) == before
    current = browser.get(f"/api/documents/{doc_id}").json()["current_version"]
    assert current["id"] == version_id
    assert current["blob"] is None
    assert current["raw_content"] == "durable parsed text"
