from datetime import timedelta

from apps.api.api.webdav import _matches_ignore_pattern, _spreadsheet_schema_is_stale
from apps.api.services.webdav import (
    RemoteEntry,
    parse_multistatus,
    validate_webdav_url,
    webdav_external_identity,
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


def test_webdav_external_identity_ignores_credentials_connector_and_default_port():
    first = webdav_external_identity(
        "https://DAV.EXAMPLE.com:443/dav/",
        "/dav/资料/a.md",
    )
    second = webdav_external_identity(
        "https://dav.example.com/another-root/",
        "/dav/%E8%B5%84%E6%96%99/./a.md",
    )
    assert first == second
    assert first != webdav_external_identity(
        "https://dav.example.com/",
        "/dav/资料/b.md",
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
    assert ".xlsx" in body[0]["include_extensions"]
    assert ".xls" in body[0]["include_extensions"]
    assert body[0]["entry_counts"] == {
        "total": 0,
        "pending": 0,
        "failed": 0,
        "synced": 0,
        "suspected_missing": 0,
        "missing": 0,
    }

    deleted = test_client.delete(f"/api/webdav/{body[0]['id']}")
    assert deleted.status_code == 200
    assert test_client.get("/api/webdav").json() == []


def test_webdav_source_can_be_updated_without_reentering_password(
    client, monkeypatch
):
    test_client, _storage = client
    source = test_client.post(
        "/api/webdav",
        json={
            "name": "个人云盘",
            "base_url": "https://dav.example.com/",
            "username": "reader",
            "password": "secret",
            "root_path": "/knowledge",
        },
    ).json()

    updated = test_client.patch(
        f"/api/webdav/{source['id']}",
        json={
            "name": "归档云盘",
            "base_url": "https://archive.example.com/dav",
            "username": "archiver",
            "password": "",
            "root_path": "/library",
            "recursive": False,
            "include_extensions": ["PDF", ".docx", "pdf"],
            "ignore_patterns": [" .* ", "*.tmp", "*.tmp"],
        },
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["name"] == "归档云盘"
    assert body["base_url"] == "https://archive.example.com/dav/"
    assert body["root_path"] == "/library"
    assert body["recursive"] is False
    assert body["include_extensions"] == [".pdf", ".docx"]
    assert body["ignore_patterns"] == [".*", "*.tmp"]
    assert body["has_password"] is True
    assert body["requires_rescan"] is True

    request = {}

    async def fake_propfind(**kwargs):
        request.update(kwargs)
        return []

    monkeypatch.setattr("apps.api.api.webdav.propfind", fake_propfind)
    tested = test_client.post(f"/api/webdav/{source['id']}/test")
    assert tested.status_code == 200
    assert request["password"] == "secret"

    cleared = test_client.patch(
        f"/api/webdav/{source['id']}",
        json={
            "name": "归档云盘",
            "base_url": "https://archive.example.com/dav",
            "username": "",
            "clear_password": True,
            "root_path": "/library",
            "recursive": False,
            "include_extensions": [".pdf"],
            "ignore_patterns": [],
        },
    )
    assert cleared.status_code == 200
    assert cleared.json()["has_password"] is False
    request.clear()
    assert test_client.post(f"/api/webdav/{source['id']}/test").status_code == 200
    assert request["password"] == ""


def test_webdav_ignore_patterns_match_path_segments():
    patterns = [".*", "~$*", "*.tmp", "@eaDir"]
    assert _matches_ignore_pattern("/docs/.draft.md", patterns)
    assert _matches_ignore_pattern("/docs/@eaDir/report.pdf", patterns)
    assert _matches_ignore_pattern("/docs/cache.tmp", patterns)
    assert not _matches_ignore_pattern("/docs/report.pdf", patterns)


def test_webdav_reprocesses_only_legacy_spreadsheet_schema():
    from types import SimpleNamespace

    legacy = SimpleNamespace(
        structured_content={
            "document_type": "xlsx",
            "metadata": {"source_format": "xlsx"},
        }
    )
    current = SimpleNamespace(
        structured_content={
            "document_type": "xlsx",
            "metadata": {"spreadsheet_schema_version": 2},
        }
    )

    assert _spreadsheet_schema_is_stale(legacy, "台账.xlsx") is True
    assert _spreadsheet_schema_is_stale(current, "台账.xlsx") is False
    assert _spreadsheet_schema_is_stale(legacy, "说明.docx") is False


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


def test_webdav_remote_missing_is_confirmed_then_trashed_and_can_be_kept(
    client, monkeypatch
):
    test_client, _storage = client
    source = test_client.post(
        "/api/webdav",
        json={
            "name": "状态保留",
            "base_url": "https://dav.example.com/",
            "username": "reader",
            "password": "secret",
            "root_path": "/knowledge",
        },
    ).json()
    remote = [
        RemoteEntry(
            path="/knowledge/missing.md",
            is_collection=False,
            etag='"v1"',
            last_modified="today",
            size=12,
            content_type="text/markdown",
        )
    ]

    async def fake_propfind(**_kwargs):
        return list(remote)

    async def fake_download(**_kwargs):
        return b"# title\nbody", "text/markdown"

    monkeypatch.setattr("apps.api.api.webdav.propfind", fake_propfind)
    monkeypatch.setattr("apps.api.api.webdav.download_file", fake_download)
    monkeypatch.setattr(
        "apps.api.api.webdav.REMOTE_MISSING_GRACE", timedelta(0)
    )
    test_client.post(f"/api/webdav/{source['id']}/scan")
    test_client.post(f"/api/webdav/{source['id']}/sync")
    entry = test_client.get(f"/api/webdav/{source['id']}/entries").json()[0]
    remote.clear()
    test_client.post(f"/api/webdav/{source['id']}/scan")
    assert test_client.get(
        f"/api/webdav/{source['id']}/entries"
    ).json()[0]["state"] == "suspected_missing"
    confirmed = test_client.post(f"/api/webdav/{source['id']}/scan").json()
    assert confirmed["trashed"] == 1
    missing_entry = test_client.get(
        f"/api/webdav/{source['id']}/entries"
    ).json()[0]
    assert missing_entry["state"] == "ignored"
    assert len(test_client.get("/api/documents?deleted=true").json()) == 1

    remote.extend(
        [
            RemoteEntry(
                path="/knowledge/missing.md",
                is_collection=False,
                etag='"v2"',
                last_modified="tomorrow",
                size=13,
                content_type="text/markdown",
            )
        ]
    )
    returned = test_client.post(f"/api/webdav/{source['id']}/scan").json()
    assert returned["restored"] == 1
    assert test_client.get(
        f"/api/webdav/{source['id']}/entries"
    ).json()[0]["state"] == "changed"

    remote.clear()
    test_client.post(f"/api/webdav/{source['id']}/scan")
    test_client.post(f"/api/webdav/{source['id']}/scan")
    missing_entry = test_client.get(
        f"/api/webdav/{source['id']}/entries"
    ).json()[0]
    assert missing_entry["state"] == "ignored"
    kept = test_client.post(
        f"/api/webdav/{source['id']}/entries/{missing_entry['id']}/keep-snapshot"
    )
    assert kept.status_code == 200
    kept_entry = test_client.get(
        f"/api/webdav/{source['id']}/entries"
    ).json()[0]
    assert kept_entry["state"] == "missing"
    assert kept_entry["keep_snapshot"] is True
    assert test_client.get("/api/documents?deleted=false").status_code == 200

    test_client.post(f"/api/webdav/{source['id']}/scan")
    kept_missing = test_client.get(
        f"/api/webdav/{source['id']}/entries"
    ).json()[0]
    assert kept_missing["state"] == "missing"
    assert kept_missing["keep_snapshot"] is True


def test_permanently_deleted_webdav_file_stays_excluded_after_reconnect(
    client, monkeypatch
):
    test_client, _storage = client
    config = {
        "name": "可重连知识源",
        "base_url": "https://dav.example.com/",
        "username": "reader",
        "password": "secret",
        "root_path": "/knowledge",
    }
    source = test_client.post("/api/webdav", json=config).json()

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
    test_client.post(f"/api/webdav/{source['id']}/scan")
    test_client.post(f"/api/webdav/{source['id']}/sync")
    entry = test_client.get(f"/api/webdav/{source['id']}/entries").json()[0]
    document_id = entry["document_id"]
    test_client.delete(f"/api/documents/{document_id}")
    assert (
        test_client.delete(
            f"/api/documents/{document_id}/permanent"
        ).status_code
        == 200
    )
    assert (
        test_client.delete(
            f"/api/webdav/{source['id']}?document_action=keep"
        ).status_code
        == 200
    )

    reconnected = test_client.post("/api/webdav", json=config).json()
    scanned = test_client.post(
        f"/api/webdav/{reconnected['id']}/scan"
    ).json()
    assert scanned["ignored"] == 1
    entry = test_client.get(
        f"/api/webdav/{reconnected['id']}/entries"
    ).json()[0]
    assert entry["state"] == "ignored"
    assert entry["ignore_reason"] == "permanent_deleted"
    assert entry["document_id"] is None
    assert (
        test_client.post(
            f"/api/webdav/{reconnected['id']}/sync"
        ).json()["imported"]
        == 0
    )

    allowed = test_client.post(
        f"/api/webdav/{reconnected['id']}/entries/{entry['id']}/allow-reimport"
    )
    assert allowed.status_code == 200
    assert (
        test_client.post(
            f"/api/webdav/{reconnected['id']}/sync"
        ).json()["imported"]
        == 1
    )


def test_connector_disable_and_delete_impact_actions(client, monkeypatch):
    test_client, _storage = client
    source = test_client.post(
        "/api/webdav",
        json={
            "name": "生命周期",
            "base_url": "https://dav.example.com/",
            "username": "reader",
            "password": "secret",
            "root_path": "/knowledge",
        },
    ).json()
    disabled = test_client.patch(
        f"/api/webdav/{source['id']}/enabled",
        json={"is_enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["is_enabled"] is False
    assert test_client.post(f"/api/webdav/{source['id']}/scan").status_code == 409
    enabled = test_client.patch(
        f"/api/webdav/{source['id']}/enabled",
        json={"is_enabled": True},
    )
    assert enabled.status_code == 200

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
    test_client.post(f"/api/webdav/{source['id']}/scan")
    test_client.post(f"/api/webdav/{source['id']}/sync")
    impact = test_client.get(
        f"/api/webdav/{source['id']}/delete-impact"
    ).json()
    assert impact["active_document_count"] == 1
    assert impact["remote_files_affected"] == 0
    deleted = test_client.delete(
        f"/api/webdav/{source['id']}?document_action=trash"
    )
    assert deleted.status_code == 200
    assert deleted.json()["remote_files_affected"] == 0
    assert len(test_client.get("/api/documents?deleted=true").json()) == 1
