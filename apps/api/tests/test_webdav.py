from apps.api.services.webdav import (
    RemoteEntry,
    parse_multistatus,
    validate_webdav_url,
)


def test_parse_webdav_multistatus():
    payload = b"""<?xml version="1.0"?>
    <d:multistatus xmlns:d="DAV:">
      <d:response><d:href>/docs/</d:href><d:propstat><d:prop>
        <d:resourcetype><d:collection/></d:resourcetype>
      </d:prop></d:propstat></d:response>
      <d:response><d:href>/docs/a.md</d:href><d:propstat><d:prop>
        <d:resourcetype/><d:getetag>"abc"</d:getetag>
        <d:getcontentlength>42</d:getcontentlength>
        <d:getcontenttype>text/markdown</d:getcontenttype>
      </d:prop></d:propstat></d:response>
    </d:multistatus>"""
    entries = parse_multistatus(payload)
    assert entries[0].is_collection is True
    assert entries[1].path == "/docs/a.md"
    assert entries[1].etag == '"abc"'
    assert entries[1].size == 42


def test_trusted_private_webdav_requires_http_url():
    assert (
        validate_webdav_url(
            "http://192.168.1.2:5005/dav",
            trusted_private_network=True,
        )
        == "http://192.168.1.2:5005/dav/"
    )


def test_created_webdav_source_is_returned_in_connector_list(client):
    test_client, _storage = client
    created = test_client.post(
        "/api/webdav",
        json={
            "name": "个人云盘",
            "base_url": "https://dav.example.com/",
            "username": "reader",
            "password": "secret",
            "root_path": "/knowledge",
        },
    )
    assert created.status_code == 201

    response = test_client.get("/api/webdav")
    assert response.status_code == 200
    body = response.json()
    assert body[0]["name"] == "个人云盘"
    assert body[0]["entry_counts"] == {
        "total": 0,
        "pending": 0,
        "failed": 0,
        "synced": 0,
    }

    deleted = test_client.delete(f"/api/webdav/{body[0]['id']}")
    assert deleted.status_code == 200
    assert test_client.get("/api/webdav").json() == []


def test_deleted_webdav_document_is_not_rediscovered(client, monkeypatch):
    test_client, _storage = client
    source = test_client.post(
        "/api/webdav",
        json={
            "name": "只读知识源",
            "base_url": "https://dav.example.com/",
            "username": "reader",
            "password": "secret",
            "root_path": "/knowledge",
        },
    ).json()

    async def fake_propfind(**_kwargs):
        return [
            RemoteEntry(
                path="/knowledge/a.md",
                is_collection=False,
                etag='"v1"',
                last_modified="today",
                size=12,
                content_type="text/markdown",
            )
        ]

    async def fake_download(**_kwargs):
        return b"# title\nbody", "text/markdown"

    monkeypatch.setattr("apps.api.api.webdav.propfind", fake_propfind)
    monkeypatch.setattr("apps.api.api.webdav.download_file", fake_download)
    monkeypatch.setattr("apps.api.api.documents.download_file", fake_download)
    assert test_client.post(f"/api/webdav/{source['id']}/scan").status_code == 200
    synced = test_client.post(f"/api/webdav/{source['id']}/sync").json()
    assert synced["imported"] == 1
    entry = test_client.get(f"/api/webdav/{source['id']}/entries").json()[0]

    downloaded = test_client.get(
        f"/api/documents/{entry['document_id']}/original"
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"# title\nbody"
    assert "a.md" in downloaded.headers["content-disposition"]

    assert test_client.delete(f"/api/documents/{entry['document_id']}").status_code == 200
    rescanned = test_client.post(f"/api/webdav/{source['id']}/scan").json()
    assert rescanned["ignored"] == 1
    entry = test_client.get(f"/api/webdav/{source['id']}/entries").json()[0]
    assert entry["state"] == "ignored"
    assert test_client.post(f"/api/webdav/{source['id']}/sync").json()["imported"] == 0
