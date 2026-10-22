import asyncio

import httpx
import pytest

from apps.api.services import webdav


def setup_http(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(webdav, 'validate_webdav_url', lambda url, **_: url)
    monkeypatch.setattr(webdav, 'normalize_url', lambda url: url)
    monkeypatch.setattr(webdav.httpx, 'AsyncClient',
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))


def download(**kwargs):
    return asyncio.run(webdav.download_file(
        base_url='https://dav.example/', remote_path='/docs/a.md', username='synthetic',
        password='synthetic', trusted_private_network=False, **kwargs))


def test_declared_size_rejected_before_body_is_read(monkeypatch):
    class Unread(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError('oversized body must not be read')
            yield b''  # pragma: no cover
    setup_http(monkeypatch, lambda _: httpx.Response(200, headers={'Content-Length': '999999'}, stream=Unread()))
    with pytest.raises(webdav.WebDAVError, match='超过'):
        download(max_bytes=100)


def test_unknown_length_stops_stream_at_limit(monkeypatch):
    seen = []
    class Large(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(20):
                seen.append(i)
                yield b'x' * 65536
    setup_http(monkeypatch, lambda _: httpx.Response(200, stream=Large()))
    with pytest.raises(webdav.WebDAVError, match='超过'):
        download(max_bytes=65536)
    assert len(seen) <= 2


def test_redirect_never_restores_auth_after_cross_origin(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={'Location': 'https://storage.example/signed'})
        if len(requests) == 2:
            return httpx.Response(302, headers={'Location': 'https://dav.example/final'})
        return httpx.Response(200, content=b'content', headers={'Content-Type': 'text/plain'})
    setup_http(monkeypatch, handler)
    assert download(max_bytes=100)[0] == b'content'
    assert 'authorization' in requests[0].headers
    assert all('authorization' not in request.headers for request in requests[1:])


def test_decoded_special_path_is_encoded_once(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=b'ok')
    setup_http(monkeypatch, handler)
    asyncio.run(webdav.download_file(base_url='https://dav.example/',
        remote_path='/docs/中文#?%2F.md', username='synthetic', password='synthetic',
        trusted_private_network=False, max_bytes=10))
    assert requests[0].url.raw_path == b'/docs/%E4%B8%AD%E6%96%87%23%3F%252F.md'
    assert not requests[0].url.query and not requests[0].url.fragment


def test_empty_body_is_an_explicit_per_file_error(monkeypatch):
    setup_http(monkeypatch, lambda _: httpx.Response(200, content=b''))
    with pytest.raises(webdav.WebDAVError, match='内容为空'):
        download(max_bytes=100)


def test_total_deadline_covers_stream_reading(monkeypatch):
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(1)
            yield b'late'
    setup_http(monkeypatch, lambda _: httpx.Response(200, stream=Slow()))
    with pytest.raises(webdav.WebDAVError, match='超时'):
        download(max_bytes=100, timeout_seconds=0.01)
