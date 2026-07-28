import socket

import pytest

from apps.api.ai.provider import AIProviderError, _parse_and_validate
from apps.api.parsers.html import HtmlParser
from apps.api.security import URLFetchError, URLSecurityError, fetch_url, normalize_url
from apps.api.security.url_safety import AllowedAddress, resolve_allowed_addresses


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/admin",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://user:pass@example.com/",
    ],
)
def test_normalize_url_rejects_unsafe_targets(url):
    with pytest.raises(URLSecurityError):
        normalize_url(url)


def test_normalize_url_canonicalizes_public_url():
    assert normalize_url("HTTPS://Example.COM:443/a?q=1#fragment") == (
        "https://example.com/a?q=1"
    )


def test_dns_with_any_private_answer_is_rejected(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
        ],
    )
    with pytest.raises(URLSecurityError, match="受限地址"):
        resolve_allowed_addresses("example.com")


class _FakeResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status = status
        self._headers = {key.lower(): value for key, value in (headers or {}).items()}
        self._body = body
        self._offset = 0

    def getheader(self, name):
        return self._headers.get(name.lower())

    def read(self, size):
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class _FakeConnection:
    def __init__(self, response):
        self.response = response

    def request(self, *args, **kwargs):
        return None

    def getresponse(self):
        return self.response

    def close(self):
        return None


def _patch_public_dns(monkeypatch):
    monkeypatch.setattr(
        "apps.api.security.url_fetch.resolve_allowed_addresses",
        lambda hostname: [AllowedAddress(socket.AF_INET, "93.184.216.34")],
    )


def test_fetch_url_stream_limit(monkeypatch):
    _patch_public_dns(monkeypatch)
    response = _FakeResponse(
        headers={"Content-Type": "text/html"},
        body=b"x" * 11,
    )
    monkeypatch.setattr(
        "apps.api.security.url_fetch._open_connection",
        lambda *args, **kwargs: _FakeConnection(response),
    )
    with pytest.raises(URLFetchError, match="大小限制"):
        fetch_url("https://example.com/", max_bytes=10)


def test_fetch_url_revalidates_redirect_target(monkeypatch):
    _patch_public_dns(monkeypatch)
    response = _FakeResponse(
        status=302,
        headers={"Location": "http://127.0.0.1/admin"},
    )
    monkeypatch.setattr(
        "apps.api.security.url_fetch._open_connection",
        lambda *args, **kwargs: _FakeConnection(response),
    )
    with pytest.raises(URLSecurityError):
        fetch_url("https://example.com/", max_bytes=1024)


def test_html_parser_extracts_visible_article_metadata():
    html = b"""<!doctype html><html lang="zh-CN"><head>
    <title>Useful article</title>
    <meta name="description" content="A useful description">
    <meta name="author" content="Alice">
    <meta property="article:published_time" content="2026-07-28">
    </head><body><nav>menu</nav><article><h1>Heading</h1>
    <p>Visible paragraph.</p><script>alert('hidden')</script></article></body></html>"""
    result = HtmlParser().parse(html, "text/html")
    assert result.success is True
    assert result.structured_content is not None
    assert result.structured_content.metadata["title"] == "Useful article"
    assert result.structured_content.metadata["author"] == "Alice"
    assert result.structured_content.metadata["published_at"] == "2026-07-28"
    assert "Visible paragraph." in result.structured_content.full_text()
    assert "hidden" not in result.structured_content.full_text()


def test_ai_schema_rejects_category_outside_whitelist():
    payload = (
        '{"summary":"摘要","category_slug":"secret","tags":["测试"],'
        '"confidence":0.8,"rationale":"原因","doc_type":"article"}'
    )
    with pytest.raises(AIProviderError, match="白名单"):
        _parse_and_validate(payload, ["tech", "inbox"])


def test_url_api_creates_async_document(client):
    test_client, _ = client
    response = test_client.post(
        "/api/sources/url",
        json={"url": "https://example.com/article#section"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["source_type"] == "url"
    assert body["source_url"] == "https://example.com/article"
    assert body["current_version"]["processing_status"] == "created"


def test_url_api_rejects_private_literal(client):
    test_client, _ = client
    response = test_client.post(
        "/api/sources/url",
        json={"url": "http://192.168.1.10/admin"},
    )
    assert response.status_code == 400
