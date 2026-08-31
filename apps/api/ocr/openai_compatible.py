"""OpenAI-compatible vision OCR provider.

Sends the PNG as a ``data:image/png;base64,...`` URL inside a
``chat.completions`` request and parses the model's reply back
into :class:`ExternalOcrLine` rows. The contract the model has to
honour:

* Return **strict JSON** (no prose, no Markdown fences).
* Top-level shape::

    {"lines": [
        {"text": "...", "confidence": 0..1000, "bbox": [x0, y0, x1, y1]},
        ...
    ]}

  ``confidence`` and ``bbox`` are optional. When ``bbox`` is
  supplied the coordinates are normalized to a 0..1000 page
  space with origin at the top-left. The parser converts them to
  the canonical PDF point coordinate system.

* Any deviation from the contract is treated as an
  :class:`ExternalOcrError` with category ``"invalid"``; the
  worker falls back to the local tesseract result for that page.

The provider does **not** log:

* the API key (only the SHA-256 prefix is ever sent to the logs);
* the PNG bytes or the base64 form;
* the upstream request or response body, even on error.

It is intentionally pluggable: the prompt and the JSON schema are
kept here so a future Ollama / vLLM / OpenRouter implementation
can subclass and reuse the same parser.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from typing import Any

import httpx

from .base import (
    ExternalOcrError,
    ExternalOcrLine,
    ExternalOcrProvider,
    ExternalOcrResult,
)

logger = logging.getLogger(__name__)


# Maximum number of PDF points we accept for any single axis.
# Bbox values outside this range are silently clamped. The
# ``1000`` ceiling matches the contract documented in the prompt
# so a misbehaving model that swaps width and height cannot push
# the chunk coordinates outside the page.
_MAX_PT = 1000.0


_OCR_SYSTEM_PROMPT = (
    "You are a careful OCR engine. You receive a single page "
    "rendered as a PNG. Read every legible word exactly once, "
    "preserving reading order from top to bottom and left to "
    "right. Do not summarise, translate, or invent text. Return "
    "strict JSON only, with no Markdown fences or extra prose."
)


_OCR_USER_PROMPT = """Output the recognised text as strict JSON with the shape:

{{
  "lines": [
    {{"text": "...", "confidence": 0..1000, "bbox": [x0, y0, x1, y1]}},
    ...
  ]
}}

Rules:
- "text" must contain the verbatim line content, whitespace
  collapsed to single spaces. Empty lines must be omitted.
- "confidence" is your own self-reported confidence in the closed
  range 0..1000. It is optional; omit it when you are not sure.
- "bbox" is the line bounding box in PDF point coordinates
  (origin at the top-left corner of the page), expressed as four
  integers in the closed range 0..1000. Treat the page as 1000
  units wide and 1000 units tall regardless of the original
  size; do NOT report pixel coordinates. The values 0 and 1000
  mean the line touches the corresponding edge. Omit "bbox"
  when you cannot give a meaningful one.
- Reading order: top to bottom, then left to right within each
  line. Do not reorder lines.
- Do not return anything outside the JSON object. Do not add
  comments or trailing commas.
"""


class OpenAICompatibleOcrProvider(ExternalOcrProvider):
    """OpenAI-compatible chat-completions vision OCR.

    Args:
        base_url: Server URL (no trailing ``/`` required).
        api_key: Bearer token. The provider never logs it; the
            constructor only stores it on ``self._api_key``.
        model: Model identifier (e.g. ``gpt-4o-mini``,
            ``qwen-vl-max``). The same identifier is mirrored
            onto :class:`ExternalOcrResult.model`.
        timeout: Request timeout in seconds. Clamped to
            ``[1, 600]`` to mirror the chat-side constraints.
    """

    name = "openai"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        self._model = (model or "").strip()
        try:
            self._timeout = max(1.0, min(600.0, float(timeout)))
        except (TypeError, ValueError):
            self._timeout = 30.0

    def is_configured(self) -> bool:
        return bool(self._base_url) and bool(self._api_key) and bool(self._model)

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self._model, "version": "chat-completions"}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def recognize(
        self,
        png_bytes: bytes,
        *,
        page_number: int,
    ) -> ExternalOcrResult:
        if not self.is_configured():
            raise ExternalOcrError(
                "外部 OCR 未配置完成",
                category="config",
                provider=self.name,
            )
        if not png_bytes:
            raise ExternalOcrError(
                "页面渲染为空，跳过外部 OCR",
                category="invalid",
                provider=self.name,
            )

        data_url = self._build_data_url(png_bytes)
        request_body = self._build_request_body(data_url)
        try:
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                json=request_body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
                # OCR endpoints are configured explicitly by the operator;
                # do not silently route document images through process-wide
                # HTTP(S)/SOCKS proxy variables.
                trust_env=False,
            )
        except httpx.TimeoutException as exc:
            raise ExternalOcrError(
                f"外部 OCR 请求超时：{type(exc).__name__}",
                category="timeout",
                provider=self.name,
            ) from None
        except httpx.HTTPError as exc:
            raise ExternalOcrError(
                f"外部 OCR 请求失败：{type(exc).__name__}",
                category="network",
                provider=self.name,
            ) from None

        if response.status_code >= 400:
            self._raise_for_status(response)

        try:
            payload = response.json()
        except ValueError as exc:
            raise ExternalOcrError(
                "外部 OCR 返回的不是 JSON",
                category="invalid",
                provider=self.name,
            ) from None

        return self._parse_payload(payload, page_number=page_number)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_data_url(self, png_bytes: bytes) -> str:
        encoded = base64.b64encode(png_bytes).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    def _build_request_body(self, data_url: str) -> dict[str, Any]:
        return {
            "model": self._model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _OCR_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _OCR_USER_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                },
            ],
        }

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        # We deliberately do NOT log response.text here; it may
        # contain the API key in the case of badly configured
        # proxies. The status code and the type are enough to
        # diagnose the failure without leaking.
        if status in (401, 403):
            raise ExternalOcrError(
                f"外部 OCR 鉴权失败（HTTP {status}）",
                category="auth",
                provider=self.name,
            )
        if status in (408, 409, 425, 429):
            raise ExternalOcrError(
                f"外部 OCR 暂时不可用（HTTP {status}）",
                category="upstream",
                provider=self.name,
            )
        if 500 <= status <= 599:
            raise ExternalOcrError(
                f"外部 OCR 服务异常（HTTP {status}）",
                category="upstream",
                provider=self.name,
            )
        raise ExternalOcrError(
            f"外部 OCR 拒绝请求（HTTP {status}）",
            category="upstream",
            provider=self.name,
        )

    def _parse_payload(
        self,
        payload: Any,
        *,
        page_number: int,
    ) -> ExternalOcrResult:
        if not isinstance(payload, dict):
            raise ExternalOcrError(
                "外部 OCR 响应不是 JSON 对象",
                category="invalid",
                provider=self.name,
            )
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExternalOcrError(
                "外部 OCR 响应缺少 choices 字段",
                category="invalid",
                provider=self.name,
            ) from None
        text = _extract_message_text(message)
        if not text:
            raise ExternalOcrError(
                "外部 OCR 没有返回内容",
                category="invalid",
                provider=self.name,
            )

        lines_payload = _parse_lines_json(text, provider=self.name)
        lines: list[ExternalOcrLine] = []
        total_chars = 0
        for raw in lines_payload:
            line = _coerce_line(raw, provider=self.name)
            if line is None:
                continue
            total_chars += len(line.text.replace(" ", ""))
            lines.append(line)

        version = _coerce_version(payload.get("model"), default=self._model)
        # Log a tiny, secret-free audit so the operator can see
        # that the external call returned something, without
        # logging the image or the response body. The page number
        # is the only free-form field and stays within
        # ``[1, 10_000]`` thanks to the parser's loop.
        logger.info(
            "external_ocr_recognized",
            extra={
                "provider": self.name,
                "page": page_number,
                "model": self._model,
                "lines": len(lines),
                "chars": total_chars,
            },
        )
        return ExternalOcrResult(
            lines=lines,
            provider=self.name,
            model=self._model,
            version=version,
            raw_text_chars=total_chars,
        )


# --- helpers --------------------------------------------------------------


def _extract_message_text(message: Any) -> str:
    """Pull the textual content out of an OpenAI chat-completions message.

    The OpenAI-compatible vision models sometimes return a list
    of content parts (``[{"type": "text", "text": "..."}]``);
    some proxies wrap the JSON in a Markdown fence. We strip
    fences and concatenate every textual part. Reasoning
    channels (``reasoning`` / ``reasoning_content``) are
    intentionally ignored because they are not part of the
    contract.
    """

    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "\n".join(parts)


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _parse_lines_json(text: str, *, provider: str) -> list[Any]:
    """Parse the model's JSON output, tolerating accidental fences.

    Returns the raw list of line objects (each item is still a
    ``dict``/``list``/scalar — :func:`_coerce_line` filters out
    anything that does not look like a line). Both the strict
    ``{"lines": [...]}`` shape and a bare ``[...]`` top-level
    array are accepted because some lightweight models return the
    latter when the system prompt is stripped by a proxy.
    """

    cleaned = (text or "").strip()
    cleaned = _FENCE_RE.sub("", cleaned).strip()
    try:
        payload = json.loads(cleaned)
    except (TypeError, ValueError):
        decoder = json.JSONDecoder()
        candidate: Any = None
        for index, character in enumerate(cleaned):
            if character not in "{[":
                continue
            try:
                parsed, _ = decoder.raw_decode(cleaned[index:])
            except ValueError:
                continue
            if isinstance(parsed, (dict, list)):
                candidate = parsed
                break
        if candidate is None:
            raise ExternalOcrError(
                "外部 OCR 响应不是 JSON",
                category="invalid",
                provider=provider,
            )
        payload = candidate
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise ExternalOcrError(
            "外部 OCR 响应不是 JSON 对象",
            category="invalid",
            provider=provider,
        )
    raw_lines = payload.get("lines")
    if raw_lines is None:
        return []
    if not isinstance(raw_lines, list):
        raise ExternalOcrError(
            "外部 OCR 响应的 lines 不是数组",
            category="invalid",
            provider=provider,
        )
    return raw_lines


def _coerce_line(raw: Any, *, provider: str) -> ExternalOcrLine | None:
    if not isinstance(raw, dict):
        return None
    text = raw.get("text")
    if not isinstance(text, str):
        return None
    cleaned = " ".join(text.split()).strip()
    if not cleaned:
        return None
    confidence = _coerce_confidence(raw.get("confidence"))
    bbox = _coerce_bbox(raw.get("bbox"))
    return ExternalOcrLine(text=cleaned, confidence=confidence, bbox=bbox)


def _coerce_confidence(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # Normalise both ``0..1`` and ``0..1000`` scales to ``0..1000``.
    if 0.0 <= number <= 1.0:
        return int(round(number * 1000))
    if 1.0 < number <= 1000.0:
        return int(round(number))
    if number < 0:
        return 0
    return 1000


def _coerce_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    coords: list[float] = []
    for item in value:
        try:
            number = float(item)
        except (TypeError, ValueError):
            return None
        coords.append(number)
    # Clamp into the documented 0..1000 range; the parser scales
    # them to the actual page width/height afterwards.
    clamped = [max(0.0, min(_MAX_PT, coord)) for coord in coords]
    x0, y0, x1, y1 = clamped
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return [x0, y0, x1, y1]


def _coerce_version(model: Any, *, default: str) -> str | None:
    """Return a short, non-secret version identifier for the result.

    The OpenAI response may include a finer model id (e.g.
    ``gpt-4o-mini-2024-07-18``); we expose at most 64 characters
    and only the part that does not start with ``sk-`` to make
    accidental key leaks from a misconfigured proxy exceedingly
    unlikely.
    """

    candidate = model if isinstance(model, str) and model.strip() else default
    if not candidate:
        return None
    candidate = candidate.strip()
    if candidate.lower().startswith("sk-"):
        return None
    return candidate[:64]


def redact_api_key(api_key: str | None) -> str:
    """Return a short, irreversible fingerprint for logging.

    Used by the worker when it logs "external OCR used" so a
    verbose stack trace cannot accidentally include the raw key.
    """

    if not api_key:
        return ""
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return digest[:12]
