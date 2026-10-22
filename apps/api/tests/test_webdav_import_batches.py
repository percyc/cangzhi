"""Tests for the cursor-driven WebDAV import batches (per-file persistence).

Also covers the shared ``download_file`` streaming behavior (early size cap and
path quoting) which the batch endpoint relies on. The legacy ``/sync``
MissingGreenlet fixes are exercised by ``test_webdav_failure_regression.py``.
"""
import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select

from apps.api.core.db import get_db
from apps.api.main import app
from apps.api.models.documents import DocumentVersion
from apps.api.models.webdav import WebDAVEntry
from apps.api.services.webdav import RemoteEntry, WebDAVError, download_file


def _db_factory():
    return asynccontextmanager(app.dependency_overrides[get_db])


def _count_versions(document_id: int) -> int:
    async def go():
        async with _db_factory()() as session:
            return int(
                (
                    await session.execute(
                        select(func.count(DocumentVersion.id)).where(
                            DocumentVersion.document_id == document_id
                        )
                    )
                ).scalar_one()
                or 0
            )

    return asyncio.run(go())


def _entry_states(http, source_id):
    return {
        item["remote_path"]: item["state"]
        for item in http.get(f"/api/webdav/{source_id}/entries").json()
    }


def _entry_ids(http, source_id):
    return {
        item["remote_path"]: item["id"]
        for item in http.get(f"/api/webdav/{source_id}/entries").json()
    }


def _create_source(http, *, root="/docs", files=("a.md", "b.md", "c.md"), etag="v1"):
    """Create a WebDAV source and a fake scan that yields the given files."""
    source = http.post(
        "/api/webdav",
        json={
            "name": "导入测试",
            "base_url": "https://dav.example/",
            "username": "reader",
            "password": "test",
            "root_path": root,
        },
    ).json()
    remote = [
        RemoteEntry(root.rstrip("/") + "/" + name, False, etag, "today", 10, "text/markdown")
        for name in files
    ]

    async def scan(**_kwargs):
        return (remote, 1, root)

    return source, remote


@pytest.fixture
def drive_import(client, monkeypatch):
    """Wire the app to a fake scan and an import-time downloader."""
    http, _storage = client
    seen = []

    def _install(downloader, *, etag="v1", files=("a.md", "b.md", "c.md"), root="/docs"):
        source, _remote = _create_source(http, root=root, files=files, etag=etag)

        async def scan(**_kwargs):
            _remote = [
                RemoteEntry(
                    root.rstrip("/") + "/" + name,
                    False,
                    etag,
                    "today",
                    10,
                    "text/markdown",
                )
                for name in files
            ]
            return (_remote, 1, root)

        monkeypatch.setattr("apps.api.api.webdav.scan_tree", scan)
        assert http.post(f"/api/webdav/{source['id']}/scan").status_code == 200

        async def dl(**kwargs):
            seen.append(kwargs["remote_path"])
            return await downloader(**kwargs)

        monkeypatch.setattr("apps.api.api.webdav.download_file", dl)
        return source

    return http, _install, seen


async def _default_downloader(**kwargs):
    return (b"# " + kwargs["remote_path"].encode(), "text/markdown")


def test_sync_batch_cursor_progress_per_file(drive_import):
    http, install, _seen = drive_import
    source = install(_default_downloader)

    r1 = http.post(f"/api/webdav/{source['id']}/sync-batch", json={"after_id": 0}).json()
    assert r1["processed"] == 1 and r1["imported"] == 1 and r1["has_more"] is True
    through = r1["through_id"]

    r2 = http.post(
        f"/api/webdav/{source['id']}/sync-batch",
        json={"after_id": r1["after_id"], "through_id": through},
    ).json()
    assert r2["processed"] == 1 and r2["imported"] == 1 and r2["has_more"] is True
    assert r2["after_id"] > r1["after_id"]

    r3 = http.post(
        f"/api/webdav/{source['id']}/sync-batch",
        json={"after_id": r2["after_id"], "through_id": through},
    ).json()
    assert r3["processed"] == 1 and r3["imported"] == 1 and r3["has_more"] is False

    states = _entry_states(http, source["id"])
    assert list(states.values()) == ["synced", "synced", "synced"]
    total = (
        r1["imported"] + r2["imported"] + r3["imported"]
    )
    assert total == 3


def test_sync_batch_cursor_advances_past_failure_and_explicit_retry(
    drive_import, monkeypatch
):
    http, install, _seen = drive_import
    source = install(_default_downloader)
    ids = _entry_ids(http, source["id"])
    a_path, b_path = "/docs/a.md", "/docs/b.md"

    async def flaky(**kwargs):
        if kwargs["remote_path"] == a_path:
            raise WebDAVError("WebDAV 文件内容为空")
        return (b"# body", "text/markdown")

    # Override the fixture's downloader so only file a fails.
    monkeypatch.setattr("apps.api.api.webdav.download_file", flaky)
    r1 = http.post(
        f"/api/webdav/{source['id']}/sync-batch", json={"after_id": 0}
    ).json()
    assert r1["failed"] == 1 and r1["after_id"] == ids[a_path] and r1["has_more"] is True
    states = _entry_states(http, source["id"])
    assert states[a_path] == "failed"

    r2 = http.post(
        f"/api/webdav/{source['id']}/sync-batch",
        json={"after_id": r1["after_id"], "through_id": r1["through_id"]},
    ).json()
    assert r2["imported"] == 1 and r2["after_id"] == ids[b_path]
    # The failed file must NOT be retried by the default cursor.
    states = _entry_states(http, source["id"])
    assert states[a_path] == "failed" and states[b_path] == "synced"

    monkeypatch.setattr("apps.api.api.webdav.download_file", _default_downloader)
    # Explicit failure retry mode re-imports the previously failed file.
    r3 = http.post(
        f"/api/webdav/{source['id']}/sync-batch",
        json={
            "after_id": 0,
            "through_id": r1["through_id"],
            "failed_only": True,
        },
    ).json()
    assert r3["imported"] == 1 and r3["has_more"] is False
    assert _entry_states(http, source["id"])[a_path] == "synced"


def test_sync_batch_unchanged_creates_no_second_version(client, monkeypatch):
    http, _storage = client
    source, _remote = _create_source(http, files=("a.md",), etag="v1")

    async def scan(**kwargs):
        return ([_remote[0]], 1, "/docs")

    monkeypatch.setattr("apps.api.api.webdav.scan_tree", scan)
    assert http.post(f"/api/webdav/{source['id']}/scan").status_code == 200

    content = b"# same body"
    calls = {"remote_path": "/docs/a.md"}

    async def dl(**kwargs):
        return content, "text/markdown"

    monkeypatch.setattr("apps.api.api.webdav.download_file", dl)
    r1 = http.post(
        f"/api/webdav/{source['id']}/sync-batch", json={"after_id": 0}
    ).json()
    assert r1["imported"] == 1
    entry = http.get(f"/api/webdav/{source['id']}/entries").json()[0]
    document_id = entry["document_id"]
    assert _count_versions(document_id) == 1

    # A rescan with a different ETag marks the entry "changed"; re-downloading
    # identical bytes must be reported as unchanged with no new version.
    async def rescan(**kwargs):
        remote = [
            RemoteEntry("/docs/a.md", False, "v2", "tomorrow", 10, "text/markdown")
        ]
        return (remote, 1, "/docs")

    monkeypatch.setattr("apps.api.api.webdav.scan_tree", rescan)
    assert http.post(f"/api/webdav/{source['id']}/scan").status_code == 200
    r2 = http.post(
        f"/api/webdav/{source['id']}/sync-batch", json={"after_id": 0}
    ).json()
    assert r2["unchanged"] == 1 and r2["processed"] == 1
    assert _count_versions(document_id) == 1


def test_sync_batch_isolates_non_default_workspace(drive_import):
    http, install, _seen = drive_import
    source = install(_default_downloader)
    assert http.post("/api/workspaces", json={"name": "别的空间", "slug": "other"}).status_code == 201
    resp = http.post(
        f"/api/webdav/{source['id']}/sync-batch",
        json={"after_id": 0},
        headers={"X-Cangzhi-Workspace": "other"},
    )
    assert resp.status_code == 404
    # Nothing was imported in the default workspace.
    states = _entry_states(http, source["id"])
    assert list(states.values()) == ["discovered", "discovered", "discovered"]


def test_sync_batch_rejects_invalid_directory_and_selection(drive_import):
    http, install, _seen = drive_import
    source = install(_default_downloader)

    # Outside the scan root -> 400.
    assert (
        http.post(
            f"/api/webdav/{source['id']}/sync-batch",
            json={"directory": "/outside/"},
        ).status_code
        == 400
    )
    # Both selectors are mutually exclusive -> 400.
    assert (
        http.post(
            f"/api/webdav/{source['id']}/sync-batch",
            json={"directory": "/docs", "entry_ids": [1]},
        ).status_code
        == 400
    )
    # Empty explicit ids select nothing, never all.
    empty = http.post(
        f"/api/webdav/{source['id']}/sync-batch",
        json={"entry_ids": []},
    ).json()
    assert empty["processed"] == 0 and empty["has_more"] is False and empty["imported"] == 0
    # Non-positive entry ids are rejected.
    assert (
        http.post(
            f"/api/webdav/{source['id']}/sync-batch",
            json={"entry_ids": [0]},
        ).status_code
        in (400, 422)
    )


# ---------------------------------------------------------------------------
# download_file streaming behavior
# ---------------------------------------------------------------------------


class _FakeStream:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *_args):
        return False


class _FakeResponse:
    def __init__(self, *, status_code=200, headers=None, content_type="text/markdown", chunks=()):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.headers.setdefault("Content-Type", content_type)
        self._chunks = list(chunks)

    async def aiter_bytes(self, chunk_size=None):
        for chunk in self._chunks:
            yield chunk


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def stream(self, method, url, headers=None, **kwargs):
        self.calls.append((method, url, dict(headers or {})))
        return _FakeStream(self._responses.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _install_http_client(monkeypatch, responses):
    import apps.api.services.webdav as svc

    client = _FakeClient(responses)
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **k: client)
    return client


def test_download_file_quotes_decoded_path(monkeypatch):
    client = _install_http_client(
        monkeypatch,
        [_FakeResponse(headers={"Content-Length": "4"}, chunks=[b"# hi"])],
    )
    payload, ctype = asyncio.run(
        download_file(
            base_url="https://dav.example/",
            remote_path="/docs/a #b.md",
            username="reader",
            password="secret",
            trusted_private_network=False,
            max_bytes=1000,
        )
    )
    url = client.calls[0][1]
    assert "/docs/a%20%23b.md" in url
    assert payload == b"# hi"


def test_download_file_caps_via_early_content_length(monkeypatch):
    _install_http_client(
        monkeypatch,
        [
            _FakeResponse(
                headers={"Content-Length": "100"},
                chunks=[b"x" * 1000],
            )
        ],
    )
    with pytest.raises(WebDAVError) as excinfo:
        asyncio.run(
            download_file(
                base_url="https://dav.example/",
                remote_path="/docs/big.md",
                username="reader",
                password="secret",
                trusted_private_network=False,
                max_bytes=10,
            )
        )
    assert "MB" in str(excinfo.value)


def test_download_file_drops_auth_on_cross_origin_redirect(monkeypatch):
    client = _install_http_client(
        monkeypatch,
        [
            _FakeResponse(status_code=302, headers={"Location": "https://cdn.example/x"}),
            _FakeResponse(headers={"Content-Length": "4"}, chunks=[b"# ok"]),
        ],
    )
    asyncio.run(
        download_file(
            base_url="https://dav.example/",
            remote_path="/docs/a.md",
            username="reader",
            password="secret",
            trusted_private_network=False,
            max_bytes=1000,
        )
    )
    first_url, first_headers = client.calls[0][1], client.calls[0][2]
    final_url, final_headers = client.calls[1][1], client.calls[1][2]
    assert first_url.startswith("https://dav.example")
    assert "Authorization" in first_headers
    assert final_url.startswith("https://cdn.example")
    assert "Authorization" not in final_headers