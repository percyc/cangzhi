import json
import socket

import pytest

from apps.api.ai.provider import AIProviderError, _parse_and_validate
from apps.api.extractors.xinhua import extract_xinhua_html
from apps.api.parsers.html import HtmlParser
from apps.api.security import (
    DESKTOP_USER_AGENT,
    WECHAT_USER_AGENT,
    FetchedPage,
    URLFetchError,
    URLSecurityError,
    browser_profiles_for_url,
    fetch_url,
    looks_like_access_block,
    normalize_url,
)
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
        self.requests = []

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

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


def test_fetch_url_uses_realistic_desktop_browser_headers(monkeypatch):
    _patch_public_dns(monkeypatch)
    connection = _FakeConnection(
        _FakeResponse(
            headers={"Content-Type": "text/html"},
            body=b"<html><body>ok</body></html>",
        )
    )
    monkeypatch.setattr(
        "apps.api.security.url_fetch._open_connection",
        lambda *args, **kwargs: connection,
    )
    fetch_url("https://example.com/article", max_bytes=1024)
    headers = connection.requests[0][1]["headers"]
    assert headers["User-Agent"] == DESKTOP_USER_AGENT
    assert headers["Sec-Fetch-Mode"] == "navigate"
    assert headers["Sec-CH-UA-Mobile"] == "?0"
    assert headers["Upgrade-Insecure-Requests"] == "1"


def test_wechat_url_prefers_embedded_mobile_browser_profile():
    profiles = browser_profiles_for_url("https://mp.weixin.qq.com/s/article")
    assert profiles[0].name == "wechat_android"
    assert profiles[0].user_agent == WECHAT_USER_AGENT
    assert profiles[0].headers["Sec-CH-UA-Mobile"] == "?1"
    assert profiles[0].headers["Referer"] == "https://mp.weixin.qq.com/"
    assert profiles[1].name == "desktop_chrome"


def test_access_challenge_page_is_detected():
    assert looks_like_access_block(
        "环境异常\n当前环境异常，完成验证后即可继续访问。\n去验证".encode()
    )
    assert not looks_like_access_block(
        "<article><h1>正常文章</h1><p>这是正文。</p></article>".encode()
    )


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


def test_xinhua_adapter_fetches_signed_article_api(monkeypatch):
    captured = {}
    article = {
        "topic": "测试标题",
        "summary": "测试摘要",
        "releasedate": "2026-07-28",
        "content": "<p>这是新华社正文内容。</p>",
    }

    def fake_fetch(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        payload = {
            "code": "0",
            "data": "var XinhuammNews ="
            + json.dumps(article, ensure_ascii=False)
            + ";",
        }
        return FetchedPage(
            url=url,
            final_url=url,
            status_code=200,
            content_type="application/json",
            raw_bytes=json.dumps(payload, ensure_ascii=False).encode(),
        )

    monkeypatch.setattr("apps.api.extractors.xinhua.fetch_url", fake_fetch)
    monkeypatch.setattr("apps.api.extractors.xinhua.time.time", lambda: 1234.5)
    html = extract_xinhua_html(
        "https://h.xinhuaxmt.com/vh512/share/13217069?newstype=1001",
        max_bytes=1024,
        timeout_seconds=5,
    )

    assert html is not None
    assert "这是新华社正文内容".encode() in html
    assert "/news-detail/13217069?docid=13217069&share=0" in captured["url"]
    headers = captured["kwargs"]["request_headers"]
    assert headers["Timestamp"] == "1234500"
    assert len(headers["Signature"]) == 64
    assert captured["kwargs"]["max_redirects"] == 0


def test_xinhua_adapter_ignores_unrecognized_urls():
    assert (
        extract_xinhua_html(
            "https://example.com/vh512/share/13217069",
            max_bytes=1024,
            timeout_seconds=5,
        )
        is None
    )


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
