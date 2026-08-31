"""Tests for the external OCR provider package.

The OpenAI-compatible provider is exercised end-to-end with a
monkey-patched ``httpx.post`` so the test never opens a real
socket. The expectations focus on the security contract: keys,
PNG bytes and raw response bodies must never end up in logs or
the returned data, and the parser must coerce arbitrary model
output into well-formed lines.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from typing import Any

import httpx
import pytest

from apps.api.ocr import (
    ExternalOcrError,
    ExternalOcrLine,
    OpenAICompatibleOcrProvider,
    build_external_ocr_provider,
)
from apps.api.security.secrets import encrypt_secret


def _png_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def patched_post(monkeypatch):
    captured: dict[str, Any] = {}

    def install(handler):
        def fake_post(url, *, json=None, headers=None, timeout=None, **_kwargs):
            captured["url"] = url
            captured["body"] = json
            captured["headers"] = headers or {}
            captured["timeout"] = timeout
            return handler(captured, url)

        monkeypatch.setattr(
            "apps.api.ocr.openai_compatible.httpx.post", fake_post
        )
        return captured

    yield install


def test_recognize_sends_data_url_and_parses_lines(patched_post):
    def handler(request, url):
        return httpx.Response(
            200,
            json={
                "model": "vision-test-2024-07-18",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "lines": [
                                        {
                                            "text": "第一行内容",
                                            "confidence": 850,
                                            "bbox": [10, 20, 200, 80],
                                        },
                                        {
                                            "text": "second line",
                                            "confidence": 0.9,
                                            "bbox": [10, 100, 200, 160],
                                        },
                                    ]
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    captured = patched_post(handler)
    provider = OpenAICompatibleOcrProvider(
        base_url="https://ocr.example.com/v1",
        api_key="sk-ocr-secret-key-aaaaaaaaaaaa",
        model="vision-test",
        timeout=5.0,
    )
    result = provider.recognize(_png_bytes(), page_number=3)

    assert captured["url"] == "https://ocr.example.com/v1/chat/completions"
    assert (
        captured["headers"].get("Authorization")
        == "Bearer sk-ocr-secret-key-aaaaaaaaaaaa"
    )
    user_msg = captured["body"]["messages"][1]
    image_part = next(
        part
        for part in user_msg["content"]
        if part.get("type") == "image_url"
    )
    data_url = image_part["image_url"]["url"]
    assert data_url.startswith("data:image/png;base64,")
    encoded = data_url.split(",", 1)[1]
    assert base64.b64decode(encoded) == _png_bytes()

    assert result.provider == "openai"
    assert result.model == "vision-test"
    assert result.version == "vision-test-2024-07-18"
    assert [line.text for line in result.lines] == [
        "第一行内容",
        "second line",
    ]
    # The provider keeps confidences in the 0..1000 range; the
    # parser rescales them to 0..1 when it stores the block.
    assert result.lines[0].confidence == 850
    assert result.lines[1].confidence == 900
    assert result.lines[0].bbox == [10.0, 20.0, 200.0, 80.0]


def test_recognize_accepts_bare_list_payload(patched_post):
    def handler(request, url):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                [{"text": "only line", "confidence": 500}],
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    patched_post(handler)
    provider = OpenAICompatibleOcrProvider(
        base_url="https://ocr.example.com/v1",
        api_key="sk-x",
        model="vision-test",
        timeout=5.0,
    )
    result = provider.recognize(_png_bytes(), page_number=1)
    assert [line.text for line in result.lines] == ["only line"]


def test_recognize_strips_markdown_fences(patched_post):
    def handler(request, url):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "```json\n"
                                + json.dumps({"lines": [{"text": "fenced line"}]})
                                + "\n```"
                            )
                        }
                    }
                ]
            },
        )

    patched_post(handler)
    provider = OpenAICompatibleOcrProvider(
        base_url="https://ocr.example.com/v1",
        api_key="sk-x",
        model="vision-test",
        timeout=5.0,
    )
    result = provider.recognize(_png_bytes(), page_number=2)
    assert [line.text for line in result.lines] == ["fenced line"]


def test_auth_failure_raises_auth_category(patched_post):
    def handler(request, url):
        return httpx.Response(401, json={"error": "bad key"})

    patched_post(handler)
    provider = OpenAICompatibleOcrProvider(
        base_url="https://ocr.example.com/v1",
        api_key="sk-x",
        model="vision-test",
        timeout=5.0,
    )
    with pytest.raises(ExternalOcrError) as exc_info:
        provider.recognize(_png_bytes(), page_number=1)
    assert exc_info.value.category == "auth"


def test_network_error_raises_network_category(patched_post):
    def handler(request, url):
        raise httpx.ConnectError("nope", request=httpx.Request("POST", url))

    patched_post(handler)
    provider = OpenAICompatibleOcrProvider(
        base_url="https://ocr.example.com/v1",
        api_key="sk-x",
        model="vision-test",
        timeout=5.0,
    )
    with pytest.raises(ExternalOcrError) as exc_info:
        provider.recognize(_png_bytes(), page_number=1)
    assert exc_info.value.category == "network"


def test_unparseable_body_raises_invalid_category(patched_post):
    def handler(request, url):
        return httpx.Response(200, text="not json at all")

    patched_post(handler)
    provider = OpenAICompatibleOcrProvider(
        base_url="https://ocr.example.com/v1",
        api_key="sk-x",
        model="vision-test",
        timeout=5.0,
    )
    with pytest.raises(ExternalOcrError) as exc_info:
        provider.recognize(_png_bytes(), page_number=1)
    assert exc_info.value.category == "invalid"


def test_unconfigured_provider_raises_config_category():
    provider = OpenAICompatibleOcrProvider(
        base_url="", api_key="", model="", timeout=5.0
    )
    with pytest.raises(ExternalOcrError) as exc_info:
        provider.recognize(_png_bytes(), page_number=1)
    assert exc_info.value.category == "config"


def test_provider_does_not_log_api_key_or_image(
    patched_post, caplog: pytest.LogCaptureFixture
):
    secret = "sk-ocr-secret-key-aaaaaaaaaaaa"

    def handler(request, url):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"lines": [{"text": "line"}]}
                            )
                        }
                    }
                ]
            },
        )

    patched_post(handler)
    provider = OpenAICompatibleOcrProvider(
        base_url="https://ocr.example.com/v1",
        api_key=secret,
        model="vision-test",
        timeout=5.0,
    )
    with caplog.at_level(logging.INFO):
        provider.recognize(_png_bytes(), page_number=1)
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in rendered
    assert "iVBOR" not in rendered


def test_factory_uses_ciphertext_only_and_rejects_disabled():
    from types import SimpleNamespace

    row = SimpleNamespace(
        ocr_provider="disabled",
        ocr_base_url=None,
        ocr_model=None,
        ocr_api_key_cipher=None,
        has_ocr_api_key=False,
        ocr_timeout_seconds=30,
    )
    assert build_external_ocr_provider(row) is None

    row = SimpleNamespace(
        ocr_provider="openai",
        ocr_base_url="https://ocr.example.com/v1",
        ocr_model="vision-test",
        ocr_api_key_cipher=encrypt_secret("sk-ocr-encrypted-key"),
        has_ocr_api_key=True,
        ocr_timeout_seconds=20,
    )
    provider = build_external_ocr_provider(row)
    assert provider is not None
    assert provider.is_configured()
    assert provider._api_key == "sk-ocr-encrypted-key"
    assert provider._model == "vision-test"
    assert provider._timeout == pytest.approx(20.0)


def test_factory_returns_none_when_cipher_missing():
    from types import SimpleNamespace

    row = SimpleNamespace(
        ocr_provider="openai",
        ocr_base_url="https://ocr.example.com/v1",
        ocr_model="vision-test",
        ocr_api_key_cipher=None,
        has_ocr_api_key=False,
        ocr_timeout_seconds=20,
    )
    assert build_external_ocr_provider(row) is None


def test_factory_skips_unknown_provider():
    from types import SimpleNamespace

    row = SimpleNamespace(
        ocr_provider="anthropic",
        ocr_base_url="https://ocr.example.com/v1",
        ocr_model="vision-test",
        ocr_api_key_cipher=encrypt_secret("sk-x"),
        has_ocr_api_key=True,
        ocr_timeout_seconds=20,
    )
    assert build_external_ocr_provider(row) is None


def test_normalised_bbox_rescales_to_page_dimensions():
    from apps.api.ocr.openai_compatible import _coerce_bbox

    bbox = _coerce_bbox([0, 0, 1000, 1000])
    assert bbox == [0.0, 0.0, 1000.0, 1000.0]
    # Out-of-range values are clamped, not rejected.
    bbox = _coerce_bbox([-50, 1500, 2000, 800])
    assert bbox == [0.0, 800.0, 1000.0, 1000.0]
    # Swapped axes are normalised.
    bbox = _coerce_bbox([800, 0, 200, 1000])
    assert bbox == [200.0, 0.0, 800.0, 1000.0]


def test_confidence_is_normalised_to_unit_scale():
    from apps.api.ocr.openai_compatible import _coerce_confidence

    assert _coerce_confidence(850) == 850
    assert _coerce_confidence(0.5) == 500
    assert _coerce_confidence(0) == 0
    assert _coerce_confidence(1200) == 1000
    assert _coerce_confidence(-10) == 0
    assert _coerce_confidence("not-a-number") is None
    assert _coerce_confidence(None) is None
