from __future__ import annotations

import hashlib
import html
import json
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from apps.api.security import URLFetchError, fetch_url

_XINHUA_HOST = "h.xinhuaxmt.com"
_SHARE_PATH = re.compile(r"^/vh512/share/(?P<document_id>[1-9][0-9]*)/?$")
_DATA_PREFIX = "var XinhuammNews ="
_JSON_TYPES = {"application/json", "text/json", "text/plain"}


def _sm3(value: str) -> str:
    try:
        digest = hashlib.new("sm3")
    except ValueError as exc:  # pragma: no cover - depends on OpenSSL build
        raise URLFetchError("当前运行环境不支持新华社接口签名") from exc
    digest.update(value.encode("utf-8"))
    return digest.hexdigest()


def _signed_headers(request: str) -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    key = _sm3("H5")
    signature = _sm3(
        f"Key={key}&Timestamp={timestamp}&Token=&Request={request}"
    )
    return {
        "Timestamp": timestamp,
        "Signature": signature,
        "Device-Access-Id": "",
    }


def _article_id(source_url: str) -> str | None:
    parts = urlsplit(source_url)
    if parts.scheme != "https" or (parts.hostname or "").lower() != _XINHUA_HOST:
        return None
    match = _SHARE_PATH.fullmatch(parts.path)
    if match is None:
        return None
    news_type = parse_qs(parts.query).get("newstype", [""])[0]
    if news_type and not news_type.isdigit():
        return None
    return match.group("document_id")


def _decode_article(raw_bytes: bytes) -> dict:
    try:
        envelope = json.loads(raw_bytes.decode("utf-8"))
        if str(envelope.get("code")) != "0":
            raise URLFetchError(envelope.get("message") or "新华社正文接口返回失败")
        wrapped = envelope.get("data")
        if not isinstance(wrapped, str) or not wrapped.startswith(_DATA_PREFIX):
            raise URLFetchError("新华社正文接口格式发生变化")
        article = json.loads(wrapped[len(_DATA_PREFIX) :].strip().rstrip(";"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
        raise URLFetchError("无法解析新华社正文数据") from exc
    if not isinstance(article, dict):
        raise URLFetchError("新华社正文数据格式不正确")
    return article


def extract_xinhua_html(
    source_url: str,
    *,
    max_bytes: int,
    timeout_seconds: float,
) -> bytes | None:
    """Return article HTML for supported Xinhua share URLs.

    The API host and path are fixed here; only a numeric article id is copied
    from the user URL. ``fetch_url`` still performs public-DNS validation and
    IP pinning, so this adapter does not weaken the generic SSRF boundary.
    """

    document_id = _article_id(source_url)
    if document_id is None:
        return None

    query = urlencode({"docid": document_id, "share": 0})
    endpoint = (
        f"https://{_XINHUA_HOST}/1017/n/newsapi/h5/news-detail/"
        f"{document_id}?{query}"
    )
    page = fetch_url(
        endpoint,
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
        max_redirects=0,
        accept="application/json",
        allowed_content_types=_JSON_TYPES,
        request_headers=_signed_headers(query),
    )
    article = _decode_article(page.raw_bytes)
    title = str(article.get("topic") or "").strip()
    content = str(article.get("content") or "").strip()
    if not title and not content:
        raise URLFetchError("新华社正文为空")

    summary = str(article.get("summary") or "").strip()
    author = str(article.get("authors") or "").strip()
    published_at = str(
        article.get("releasedate")
        or article.get("releaseTimestamp")
        or ""
    ).strip()
    metadata = [
        "<meta charset=\"utf-8\">",
        f"<title>{html.escape(title)}</title>",
        f'<link rel="canonical" href="{html.escape(source_url, quote=True)}">',
    ]
    if summary:
        metadata.append(
            f'<meta name="description" content="{html.escape(summary, quote=True)}">'
        )
    if author:
        metadata.append(
            f'<meta name="author" content="{html.escape(author, quote=True)}">'
        )
    if published_at:
        metadata.append(
            '<meta property="article:published_time" '
            f'content="{html.escape(published_at, quote=True)}">'
        )
    return (
        "<!doctype html><html lang=\"zh-CN\"><head>"
        + "".join(metadata)
        + "</head><body><article><h1>"
        + html.escape(title)
        + "</h1>"
        + content
        + "</article></body></html>"
    ).encode("utf-8")
