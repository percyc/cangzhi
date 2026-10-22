import asyncio

import pytest

from apps.api.services.webdav import (
    RemoteEntry, WebDAVError, in_scan_scope, parse_multistatus, propfind, scan_tree,
)


def entry(path, directory=False):
    return RemoteEntry(path, directory, "v1", None, 12, "text/markdown")


async def scan(loader, **kwargs):
    return await scan_tree(base_url="https://dav.example/dav/", root_path="/docs",
        username="reader", password="test", trusted_private_network=False,
        recursive=kwargs.pop("recursive", True), ignore_patterns=[".*"],
        list_directory=loader, **kwargs)


@pytest.mark.asyncio
async def test_recursive_all_levels_base_prefix_unicode_and_duplicates():
    calls = []
    tree = {
        "/dav/docs": [entry("/dav/docs/", True), entry("/dav/docs/资料", True),
            entry("/dav/docs/资料/", True), entry("/dav/docs/.hidden", True)],
        "/dav/docs/资料": [entry("/dav/docs/资料/deep", True)],
        "/dav/docs/资料/deep": [entry("/dav/docs/资料/deep/a%.md")],
    }
    async def loader(**kwargs):
        assert kwargs["absolute_path"] is True
        assert kwargs["depth"] == "1"
        calls.append(kwargs["root_path"])
        return tree[kwargs["root_path"]]
    files, directories, root = await scan(loader)
    assert [f.path for f in files] == ["/dav/docs/资料/deep/a%.md"]
    assert directories == 3
    assert root == "/dav/docs"
    assert len(calls) == len(set(calls))
    files, directories, _ = await scan(loader, recursive=False)
    assert files == [] and directories == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"max_directories": 1}, {"max_entries": 1}])
async def test_scan_limits_fail_closed(kwargs):
    async def loader(**_):
        return [entry("/dav/docs/sub", True), entry("/dav/docs/a.md")]
    with pytest.raises(WebDAVError, match="扫描未完成"):
        await scan(loader, **kwargs)


@pytest.mark.asyncio
async def test_timeout_fails_closed():
    async def loader(**_):
        await asyncio.sleep(1)
    with pytest.raises(WebDAVError, match="超时"):
        await scan(loader, timeout_seconds=0.001)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/outside/a.md", "/dav/docs2/a.md", "/dav/docs/../a.md"])
async def test_scan_rejects_outside_entries(path):
    async def loader(**_):
        return [entry(path)]
    with pytest.raises(WebDAVError, match="越界"):
        await scan(loader)


@pytest.mark.parametrize("xml", [
    '<html/>',
    '<d:multistatus xmlns:d="DAV:"><d:response><d:href>/a</d:href><d:status>HTTP/1.1 403 Forbidden</d:status></d:response></d:multistatus>',
    '<d:multistatus xmlns:d="DAV:"><d:response><d:href>/a</d:href></d:response></d:multistatus>',
])
def test_invalid_or_partial_list_is_not_success(xml):
    with pytest.raises(WebDAVError):
        parse_multistatus(xml.encode())


def test_scope_shrink_and_ignored_descendants():
    assert in_scan_scope("/docs/sub/a.md", "/docs", True, [])
    assert not in_scan_scope("/docs/sub/a.md", "/docs", False, [])
    assert not in_scan_scope("/docs/sub/a.md", "/other", True, [])
    assert not in_scan_scope("/docs/sub/a.md", "/docs", True, ["sub"])
    assert not in_scan_scope("/docs2/a.md", "/docs", True, [])


@pytest.mark.asyncio
async def test_propfind_absolute_path_does_not_duplicate_base_or_interpret_filename(monkeypatch):
    import httpx
    original_client = httpx.AsyncClient
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(207, content=b'<d:multistatus xmlns:d="DAV:"/>')
    monkeypatch.setattr("apps.api.services.webdav.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    await propfind(base_url="https://dav.example/dav/", root_path="/dav/docs/资料%?#/",
        username="reader", password="test", trusted_private_network=True, absolute_path=True)
    assert requests[0].url.raw_path == b"/dav/docs/%E8%B5%84%E6%96%99%25%3F%23/"
    assert requests[0].headers["Depth"] == "1"


def test_optional_property_failure_does_not_hide_valid_resource():
    entries = parse_multistatus(b'''<d:multistatus xmlns:d="DAV:"><d:response>
      <d:href>/docs/sub/</d:href><d:propstat><d:prop><d:getetag/></d:prop>
      <d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>
      <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
      </d:response></d:multistatus>''')
    assert entries[0].is_collection


@pytest.mark.asyncio
async def test_oversized_directory_response_fails_closed(monkeypatch):
    import httpx
    original_client = httpx.AsyncClient
    monkeypatch.setattr("apps.api.services.webdav.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(
            lambda _: httpx.Response(207, content=b"x" * (16 * 1024 * 1024 + 1))
        ), **kwargs))
    with pytest.raises(WebDAVError, match="16 MiB"):
        await propfind(base_url="https://dav.example/", root_path="/docs",
            username="reader", password="test", trusted_private_network=True)


def test_api_recursive_failure_and_scope_changes_preserve_entries(client, monkeypatch):
    test_client, _ = client
    source = test_client.post("/api/webdav", json={
        "name": "recursive", "base_url": "https://dav.example/", "root_path": "/docs",
        "username": "reader", "password": "test",
    }).json()
    prefix = f"/api/webdav/{source['id']}"
    fail = False
    async def loader(**kwargs):
        if kwargs["root_path"] == "/docs":
            return [entry("/docs/sub", True), entry("/docs/root.md")]
        if fail:
            raise WebDAVError("子目录无权限")
        return [entry("/docs/sub/deep.md")]
    monkeypatch.setattr("apps.api.api.webdav.propfind", loader)
    result = test_client.post(prefix + "/scan")
    assert result.status_code == 200
    assert result.json()["scanned_directories"] == 2
    before = test_client.get(prefix + "/entries").json()
    assert len(before) == 2
    fail = True
    assert test_client.post(prefix + "/scan").status_code == 502
    assert test_client.get(prefix + "/entries").json() == before
    for changes in [{"recursive": False}, {"recursive": True, "ignore_patterns": ["sub"]}]:
        updated = test_client.patch(prefix, json={
            "name": "recursive", "base_url": "https://dav.example/", "root_path": "/docs",
            "username": "reader", "password": "", **changes,
        })
        assert updated.status_code == 200
        for _ in range(2):
            result = test_client.post(prefix + "/scan")
            assert result.status_code == 200
            assert result.json()["suspected_missing"] == 0
        rows = test_client.get(prefix + "/entries").json()
        assert all(row["state"] == "discovered" for row in rows)
