from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Iterable

import httpx

from ..core.config import settings
from .schema import UnderstandingResult


UNDERSTANDING_PROMPT = """你是一名中文个人知识库助理。下面是用户保存的一段内容（可能来自网页、文件或随手记）。

可用分类（仅限下列 slug）：
{category_lines}

请严格输出 JSON，不要添加任何额外文字。必须包含以下字段：
- summary: 100-300 字的简要概括，保留关键事实和结论。
- category_slug: 上述分类列表中的一项，使用 slug。
- tags: 1-5 个简洁的标签，使用中文或英文短语，避免重复。
- confidence: 0 到 1 之间的小数，数值越高代表模型越确定。
- rationale: 一句话说明主要判断依据。
- doc_type: 取值为 article / note / tweet / other。

内容：
{content}
"""


class AIProviderError(RuntimeError):
    """Raised when an AI provider call fails or returns invalid output."""


class AIProvider(ABC):
    name: str = ""

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    def generate_understanding(
        self,
        *,
        content: str,
        title: str | None,
        category_slugs: Iterable[str],
        category_display: dict[str, str],
    ) -> UnderstandingResult: ...


def _format_categories(
    category_slugs: Iterable[str],
    category_display: dict[str, str],
) -> str:
    return "\n".join(
        f"- {slug}: {category_display.get(slug, slug)}" for slug in category_slugs
    )


def _build_prompt(
    *,
    content: str,
    title: str | None,
    category_slugs: Iterable[str],
    category_display: dict[str, str],
) -> str:
    return UNDERSTANDING_PROMPT.format(
        category_lines=_format_categories(category_slugs, category_display),
        content=(f"标题：{title}\n\n" if title else "") + content[:6000],
    )


def _validate_category_slug(
    slug: str,
    allowed: set[str],
) -> str:
    if slug not in allowed:
        raise AIProviderError(f"模型返回了不在白名单中的分类：{slug}")
    return slug


def _coerce_payload(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise AIProviderError("模型输出不是 JSON 对象")
    return payload


class OpenAICompatibleProvider(AIProvider):
    name = "openai"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    def is_configured(self) -> bool:
        return bool(self._api_key) and bool(self._base_url) and bool(self._model)

    def generate_understanding(
        self,
        *,
        content: str,
        title: str | None,
        category_slugs: Iterable[str],
        category_display: dict[str, str],
    ) -> UnderstandingResult:
        if not self.is_configured():
            raise AIProviderError("未配置 OpenAI-compatible API Key")
        prompt = _build_prompt(
            content=content,
            title=title,
            category_slugs=category_slugs,
            category_display=category_display,
        )
        url = f"{self._base_url}/chat/completions"
        body = {
            "model": self._model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": "你只输出严格 JSON，不输出额外文字。",
                },
                {"role": "user", "content": prompt},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = httpx.post(
                url,
                json=body,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise AIProviderError(f"调用模型失败：{exc}") from None
        if response.status_code >= 400:
            raise AIProviderError(
                f"模型服务返回 {response.status_code}：{response.text[:200]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise AIProviderError("模型响应不是 JSON") from None
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIProviderError("模型响应缺少 choices 字段") from None
        return _parse_and_validate(text, category_slugs)


class OllamaProvider(AIProvider):
    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    def is_configured(self) -> bool:
        return bool(self._base_url) and bool(self._model)

    def generate_understanding(
        self,
        *,
        content: str,
        title: str | None,
        category_slugs: Iterable[str],
        category_display: dict[str, str],
    ) -> UnderstandingResult:
        if not self.is_configured():
            raise AIProviderError("未配置 Ollama 模型")
        prompt = _build_prompt(
            content=content,
            title=title,
            category_slugs=category_slugs,
            category_display=category_display,
        )
        url = f"{self._base_url}/api/chat"
        body = {
            "model": self._model,
            "stream": False,
            "format": "json",
            "messages": [
                {
                    "role": "system",
                    "content": "你只输出严格 JSON，不输出额外文字。",
                },
                {"role": "user", "content": prompt},
            ],
        }
        try:
            response = httpx.post(url, json=body, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise AIProviderError(f"调用 Ollama 失败：{exc}") from None
        if response.status_code >= 400:
            raise AIProviderError(
                f"Ollama 返回 {response.status_code}：{response.text[:200]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise AIProviderError("Ollama 响应不是 JSON") from None
        try:
            text = data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise AIProviderError("Ollama 响应缺少 message.content") from None
        return _parse_and_validate(text, category_slugs)


def _parse_and_validate(
    text: str,
    category_slugs: Iterable[str],
) -> UnderstandingResult:
    if not text or not text.strip():
        raise AIProviderError("模型返回空字符串")
    cleaned = text.strip()
    # Allow accidental ```json ... ``` fences.
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, count=1).strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AIProviderError(f"模型输出无法解析为 JSON：{exc}") from None
    payload = _coerce_payload(payload)
    try:
        result = UnderstandingResult.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — Pydantic errors vary
        raise AIProviderError(f"模型输出不符合 Schema：{exc}") from None
    allowed = {slug.lower() for slug in category_slugs}
    result.category_slug = _validate_category_slug(result.category_slug, allowed)
    return result


import re  # noqa: E402 — keep at the bottom to reuse above aliases


def build_provider() -> AIProvider | None:
    """Return a configured provider based on the current settings, or None."""

    provider = (settings.ai_provider or "").lower()
    if provider == "openai":
        if not settings.openai_api_key:
            return None
        return OpenAICompatibleProvider(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            timeout=settings.ai_request_timeout_seconds,
        )
    if provider == "ollama":
        if not settings.ollama_base_url:
            return None
        return OllamaProvider(
            base_url=settings.ollama_base_url,
            model=settings.ollama_model,
            timeout=settings.ai_request_timeout_seconds,
        )
    return None
