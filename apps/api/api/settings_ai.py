"""Web-managed AI runtime configuration.

The page at ``/settings`` edits a single row in
``ai_runtime_configs``; this router owns the read/update/test
contract.

* The API key is encrypted at rest with the project-wide master
  key (see :mod:`apps.api.security.secrets`). The page only ever
  sees a ``has_api_key`` boolean, never the ciphertext, the
  plaintext, or any hint of the key.
* ``POST /api/settings/ai/test`` performs a harmless probe
  against the configured endpoint. Provider responses are
  sanitised so a verbose upstream error cannot leak the user's
  API key or other secrets.
* The router never returns the configured key, even when the
  request fails.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..models.auth import Admin, AIRuntimeConfig
from ..models.documents import Document
from ..models.processing import ProcessingJob
from ..models.taxonomy import DocumentCategory, DocumentSummary, DocumentTag
from ..security.secrets import (
    SecretDecryptError,
    SecretStoreError,
    decrypt_secret,
    encrypt_secret,
)
from .auth import require_admin


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings/ai", tags=["settings"])


_PROVIDER_VALUES = ("disabled", "openai", "ollama")
_SINGLETON_KEY = "singleton"

_URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)


def _validate_url(value: str) -> str:
    cleaned = (value or "").strip().rstrip("/")
    if not _URL_RE.match(cleaned):
        raise ValueError("地址格式不正确")
    return cleaned


def _validate_model(value: str) -> str:
    cleaned = (value or "").strip()
    if len(cleaned) > 200:
        raise ValueError("模型名称不能超过 200 个字符")
    return cleaned


def _extract_model_ids(payload: Any, *, list_key: str) -> list[str]:
    """Parse a compatible model-list response without retaining upstream data."""

    if not isinstance(payload, dict):
        raise ValueError("top-level response is not an object")
    items = payload.get(list_key)
    if not isinstance(items, list):
        raise ValueError("model list is missing")

    model_ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate = item.get("id") or item.get("name") or item.get("model")
        if candidate is None:
            continue
        model_id = str(candidate).strip()
        if model_id:
            model_ids.append(model_id[:255])
    return model_ids


def _natural_model_key(model_id: str) -> list[tuple[int, int | str]]:
    """Sort model-2 before model-10 while remaining case-insensitive."""

    return [
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", model_id)
        if part
    ]


class _OpenAIConfig(BaseModel):
    base_url: str = Field(min_length=1, max_length=512)
    model: str = Field(default="", max_length=255)

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _validate_url(value)

    @field_validator("model")
    @classmethod
    def _model_name(cls, value: str) -> str:
        return _validate_model(value)


class _OllamaConfig(BaseModel):
    base_url: str = Field(min_length=1, max_length=512)
    model: str = Field(default="", max_length=255)

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _validate_url(value)

    @field_validator("model")
    @classmethod
    def _model_name(cls, value: str) -> str:
        return _validate_model(value)


class AIConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=16)
    openai: _OpenAIConfig | None = None
    ollama: _OllamaConfig | None = None
    api_key_action: str = Field(default="keep", max_length=16)
    api_key: str | None = Field(default=None, max_length=512)
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    embedding_model: str | None = Field(default=None, max_length=255)

    @field_validator("provider")
    @classmethod
    def _provider(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()
        if cleaned not in _PROVIDER_VALUES:
            raise ValueError("不支持的模型来源")
        return cleaned

    @field_validator("api_key_action")
    @classmethod
    def _action(cls, value: str) -> str:
        cleaned = (value or "keep").strip().lower()
        if cleaned not in {"keep", "replace", "clear"}:
            raise ValueError("不支持的密钥操作")
        return cleaned

    @field_validator("embedding_model")
    @classmethod
    def _embedding_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if len(cleaned) > 200:
            raise ValueError("Embedding 模型名称不能超过 200 个字符")
        return cleaned


# --- Helpers --------------------------------------------------------------


async def _get_or_create_config(db: AsyncSession) -> AIRuntimeConfig:
    row = (
        await db.execute(
            select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc()).limit(1)
        )
    ).scalars().first()
    if row is not None:
        return row
    row = AIRuntimeConfig(
        provider="disabled",
        timeout_seconds=30,
        prompt_version="v1",
        singleton_key=_SINGLETON_KEY,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # Another request raced us to create the singleton row.
        await db.rollback()
        row = (
            await db.execute(
                select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc()).limit(1)
            )
        ).scalars().first()
        if row is None:
            raise
        return row
    await db.refresh(row)
    return row


def _to_public(row: AIRuntimeConfig) -> dict[str, Any]:
    return row.to_public_dict()


def _provider_ready_for_understanding(row: AIRuntimeConfig) -> bool:
    if row.provider == "openai":
        return bool(row.openai_base_url and row.openai_model and row.has_api_key)
    if row.provider == "ollama":
        return bool(row.ollama_base_url and row.ollama_model)
    return False


async def _enqueue_unclassified_documents(
    db: AsyncSession,
    row: AIRuntimeConfig,
) -> int:
    """Queue AI organization for current versions that never got a real category."""

    if not _provider_ready_for_understanding(row):
        return 0

    documents = (
        await db.execute(
            select(Document).where(
                Document.is_deleted.is_(False),
                Document.current_version_id.is_not(None),
            )
        )
    ).scalars().all()
    queued = 0
    for document in documents:
        version_id = document.current_version_id
        if version_id is None:
            continue
        links = (
            await db.execute(
                select(DocumentCategory).where(
                    DocumentCategory.document_version_id == version_id
                )
            )
        ).scalars().all()
        if links and any(link.source != "fallback" for link in links):
            continue

        await db.execute(
            delete(DocumentCategory).where(
                DocumentCategory.document_version_id == version_id
            )
        )
        await db.execute(
            delete(DocumentSummary).where(
                DocumentSummary.document_version_id == version_id
            )
        )
        await db.execute(
            delete(DocumentTag).where(DocumentTag.document_version_id == version_id)
        )

        job = (
            await db.execute(
                select(ProcessingJob).where(
                    ProcessingJob.document_version_id == version_id,
                    ProcessingJob.stage == "understanding",
                )
            )
        ).scalars().first()
        if job is None:
            job = ProcessingJob(
                document_id=document.id,
                document_version_id=version_id,
                stage="understanding",
                status="created",
                idempotency_key=f"{version_id}:understanding:understanding:v1",
                config_version="understanding:v1",
            )
        else:
            job.status = "created"
            job.retry_count = 0
            job.next_retry_at = None
            job.last_error = None
            job.error_details = None
            job.started_at = None
            job.finished_at = None
        db.add(job)
        queued += 1

    if queued:
        await db.commit()
    return queued


# --- Endpoints ------------------------------------------------------------


@router.get("", response_model=dict[str, Any])
async def get_settings(
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(require_admin),
) -> dict[str, Any]:
    """Return the current AI runtime configuration.

    Sensitive material is intentionally omitted: the response only
    includes a ``has_api_key`` boolean.
    """

    row = await _get_or_create_config(db)
    return {"config": _to_public(row)}


@router.patch("", response_model=dict[str, Any])
async def update_settings(
    payload: AIConfigUpdate,
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(require_admin),
) -> dict[str, Any]:
    """Apply the form submission to the active AI runtime config."""

    row = await _get_or_create_config(db)
    row.provider = payload.provider
    if payload.timeout_seconds is not None:
        row.timeout_seconds = payload.timeout_seconds
    # Preserve the setting for older clients that do not send this new field.
    if "embedding_model" in payload.model_fields_set:
        row.embedding_model = payload.embedding_model

    if payload.provider == "openai":
        if payload.openai is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "openai_missing", "message": "请填写 OpenAI-compatible 配置"},
            )
        row.openai_base_url = payload.openai.base_url
        row.openai_model = payload.openai.model or None
        action = payload.api_key_action
        if action == "replace":
            if not payload.api_key:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "api_key_required", "message": "请输入要保存的密钥"},
                )
            row.openai_api_key_cipher = encrypt_secret(payload.api_key)
            row.has_api_key = True
        elif action == "clear":
            row.openai_api_key_cipher = None
            row.has_api_key = False
        # else "keep": leave existing values alone.
    if payload.provider == "ollama":
        if payload.ollama is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "ollama_missing", "message": "请填写 Ollama 配置"},
            )
        row.ollama_base_url = payload.ollama.base_url
        row.ollama_model = payload.ollama.model or None
    row.updated_by = admin.id
    db.add(row)
    await db.commit()
    await db.refresh(row)
    queued = await _enqueue_unclassified_documents(db, row)
    logger.info(
        "ai_runtime_updated provider=%s admin=%s key_set=%s organize_queued=%s",
        row.provider,
        admin.username,
        row.has_api_key,
        queued,
    )
    return {"config": _to_public(row)}


@router.post("/test", response_model=dict[str, Any])
async def test_settings(
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(require_admin),
) -> dict[str, Any]:
    """Probe the active provider without leaking sensitive data.

    A successful probe returns ``{"ok": true}``. A failed probe
    returns ``{"ok": false, "message": "..."}`` with the upstream
    body filtered so an API key embedded in a verbose error
    message cannot reach the front end.
    """

    row = await _get_or_create_config(db)
    if row.provider == "disabled":
        return {"ok": False, "message": "当前未启用任何模型来源"}
    if row.provider == "openai":
        if not row.openai_base_url or not row.openai_model:
            return {"ok": False, "message": "尚未配置 OpenAI 兼容地址或模型"}
        if not row.has_api_key or not row.openai_api_key_cipher:
            return {"ok": False, "message": "尚未保存 OpenAI 兼容密钥"}
        try:
            api_key = decrypt_secret(row.openai_api_key_cipher)
        except (SecretStoreError, SecretDecryptError):
            logger.warning("ai_test_decrypt_failed admin=%s", admin.username)
            return {"ok": False, "message": "主密钥不匹配或密文已损坏，请联系管理员"}
        if not api_key:
            return {"ok": False, "message": "尚未保存 OpenAI 兼容密钥"}
        return await _probe_openai(
            base_url=row.openai_base_url,
            model=row.openai_model,
            api_key=api_key,
            timeout=float(row.timeout_seconds or 30),
        )
    if row.provider == "ollama":
        if not row.ollama_base_url or not row.ollama_model:
            return {"ok": False, "message": "尚未配置 Ollama 地址或模型"}
        return await _probe_ollama(
            base_url=row.ollama_base_url,
            model=row.ollama_model,
            timeout=float(row.timeout_seconds or 30),
        )
    return {"ok": False, "message": "不支持的模型来源"}


async def _probe_openai(
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return _safe_failure("无法连接 OpenAI 兼容服务", exc)

    if response.status_code >= 400:
        return {
            "ok": False,
            "message": f"服务返回 HTTP {response.status_code}，请检查地址、密钥和模型权限",
        }

    try:
        data = response.json()
    except ValueError:
        return {
            "ok": False,
            "message": "服务返回的不是模型列表，请检查 API 地址是否包含正确的版本路径",
        }

    try:
        model_ids = _extract_model_ids(data, list_key="data")
    except ValueError:
        return {
            "ok": False,
            "message": "服务返回的模型列表格式不正确",
        }

    if not model_ids:
        return {"ok": False, "message": "服务没有返回任何可用模型"}

    if model.strip() not in model_ids:
        matching = [m for m in model_ids if model.strip().lower() in m.lower()]
        if matching:
            suggestions = ", ".join(m[:30] for m in matching[:3])
            msg = f"配置的模型 '{model[:30]}...' 不在模型列表中，找到相似模型：{suggestions}"
        else:
            available = ", ".join(m[:20] for m in model_ids[:5])
            if len(model_ids) > 5:
                available += "..."
            msg = f"配置的模型不在列表中，可用模型：{available}"
        return {
            "ok": False,
            "message": msg,
        }

    return {"ok": True, "message": "连接正常，模型已验证"}


async def _probe_ollama(
    *,
    base_url: str,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        return _safe_failure("无法连接 Ollama 服务", exc)

    # Check that response is actually JSON
    try:
        data = response.json()
    except ValueError:
        return {
            "ok": False,
            "message": "Ollama 返回了非 JSON 响应，请检查服务地址是否正确",
        }

    if response.status_code >= 400:
        return {
            "ok": False,
            "message": f"Ollama 返回 HTTP {response.status_code}，请检查服务地址",
        }

    try:
        model_names = _extract_model_ids(data, list_key="models")
    except ValueError:
        return {"ok": False, "message": "Ollama 返回的模型列表格式不正确"}

    if not model_names:
        return {"ok": True, "message": "连接成功（无法验证模型列表）"}

    if model.strip() not in model_names:
        available = ", ".join(m[:30] for m in model_names[:5])
        if len(model_names) > 5:
            available += "..."
        return {
            "ok": False,
            "message": f"配置的模型 '{model[:30]}...' 不在可用列表中，可用：{available}",
        }

    return {"ok": True, "message": "连接正常，模型已验证"}


def _safe_failure(prefix: str, exc: Exception) -> dict[str, Any]:
    # Never leak exception strings that may contain API keys or secrets
    # Only report the exception type, no details
    return {"ok": False, "message": f"{prefix}：{type(exc).__name__}"}


# --- Embedding connection test --------------------------------------------
#
# The embedding test is intentionally isolated from the chat test:
# the chat path calls ``/chat/completions`` (OpenAI) or ``/api/chat``
# (Ollama), but the embedding endpoints have a different shape, so
# their own probe is needed. The probe is read-only: it sends a
# single very short test string and validates that the response
# contains a non-empty, finite numeric vector. When the user has
# not picked an embedding model yet the endpoint refuses with a
# gentle hint and the existing FTS path is untouched.


_EMBEDDING_PROBE_TEXT = "ping"


def _validate_embedding_vector(value: Any) -> int:
    """Return the dimensionality of a single, well-formed embedding vector.

    ``value`` is whatever the provider returned for the first row of
    the probe input. We require a non-empty list of numbers, each of
    which must be finite (no NaN, no inf). The dimensionality is
    reported back to the operator so they can sanity-check the
    configuration before vector search ships.
    """

    if not isinstance(value, list) or not value:
        raise ValueError("返回的向量为空或不是数组")
    for item in value:
        if not isinstance(item, (int, float)) or isinstance(item, bool):
            raise ValueError("向量包含非数值字段")
        if isinstance(item, float) and (item != item or item in (float("inf"), float("-inf"))):
            raise ValueError("向量包含非法数值（NaN/Inf）")
    return len(value)


def _extract_openai_embedding(payload: Any) -> list:
    if not isinstance(payload, dict):
        raise ValueError("top-level response is not an object")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("响应缺少 data 字段")
    first = data[0]
    if not isinstance(first, dict):
        raise ValueError("响应 data[0] 不是对象")
    return first.get("embedding")


def _extract_ollama_embedding(payload: Any) -> list:
    if not isinstance(payload, dict):
        raise ValueError("top-level response is not an object")
    embeddings = payload.get("embeddings")
    if isinstance(embeddings, list) and embeddings:
        first = embeddings[0]
        if isinstance(first, list):
            return first
    # Older Ollama releases use ``embedding`` (singular) under the
    # legacy ``/api/embeddings`` endpoint. The new ``/api/embed``
    # endpoint always uses ``embeddings``; we keep the fallback so
    # a stale proxy does not surface as a hard error.
    legacy = payload.get("embedding")
    if isinstance(legacy, list) and legacy:
        return legacy
    raise ValueError("响应缺少 embeddings/embedding 字段")


@router.post("/embedding/test", response_model=dict[str, Any])
async def test_embedding(
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(require_admin),
) -> dict[str, Any]:
    """Probe the embedding endpoint configured for the active provider.

    The probe sends a one-word string (``"ping"``) and validates that
    the response carries a non-empty, finite numeric vector. The
    endpoint refuses with a clear message when:

    * the AI provider is disabled;
    * no embedding model has been selected yet;
    * the user picked an embedding model but the corresponding base
      URL or API key is missing.
    """

    row = await _get_or_create_config(db)
    if not row.embedding_model:
        return {"ok": False, "message": "尚未选择 Embedding 模型，留空即关闭"}
    if row.provider == "disabled":
        return {"ok": False, "message": "当前未启用任何模型来源"}
    if row.provider == "openai":
        if not row.openai_base_url:
            return {"ok": False, "message": "尚未配置 OpenAI 兼容地址"}
        if not row.has_api_key or not row.openai_api_key_cipher:
            return {"ok": False, "message": "尚未保存 OpenAI 兼容密钥"}
        try:
            api_key = decrypt_secret(row.openai_api_key_cipher)
        except (SecretStoreError, SecretDecryptError):
            logger.warning("ai_embedding_test_decrypt_failed admin=%s", admin.username)
            return {"ok": False, "message": "主密钥不匹配或密文已损坏，请联系管理员"}
        if not api_key:
            return {"ok": False, "message": "尚未保存 OpenAI 兼容密钥"}
        return await _probe_openai_embedding(
            base_url=row.openai_base_url,
            model=row.embedding_model,
            api_key=api_key,
            timeout=float(row.timeout_seconds or 30),
        )
    if row.provider == "ollama":
        if not row.ollama_base_url:
            return {"ok": False, "message": "尚未配置 Ollama 地址"}
        return await _probe_ollama_embedding(
            base_url=row.ollama_base_url,
            model=row.embedding_model,
            timeout=float(row.timeout_seconds or 30),
        )
    return {"ok": False, "message": "不支持的模型来源"}


async def _probe_openai_embedding(
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/embeddings"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {"model": model, "input": _EMBEDDING_PROBE_TEXT}
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        return _safe_failure("无法连接 Embedding 服务", exc)

    if response.status_code >= 400:
        return {
            "ok": False,
            "message": f"服务返回 HTTP {response.status_code}，请检查地址、密钥和模型权限",
        }

    try:
        data = response.json()
    except ValueError:
        return {
            "ok": False,
            "message": "Embedding 服务返回的不是 JSON",
        }

    try:
        vector = _extract_openai_embedding(data)
        dim = _validate_embedding_vector(vector)
    except ValueError as exc:
        return {"ok": False, "message": f"Embedding 响应无效：{exc}"}

    return {
        "ok": True,
        "message": f"连接正常，Embedding 模型已验证（维度 {dim}）",
    }


async def _probe_ollama_embedding(
    *,
    base_url: str,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/embed"
    body = {"model": model, "input": _EMBEDDING_PROBE_TEXT}
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(url, json=body)
    except httpx.HTTPError as exc:
        return _safe_failure("无法连接 Ollama Embedding 服务", exc)

    if response.status_code >= 400:
        # Some older Ollama builds only expose ``/api/embeddings``.
        # Fall back to that legacy path so the user still gets a
        # useful result instead of a hard error.
        if response.status_code in (404, 405):
            return await _probe_ollama_legacy_embedding(
                base_url=base_url, model=model, timeout=timeout
            )
        return {
            "ok": False,
            "message": f"Ollama Embedding 返回 HTTP {response.status_code}",
        }

    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "message": "Ollama Embedding 返回的不是 JSON"}

    try:
        vector = _extract_ollama_embedding(data)
        dim = _validate_embedding_vector(vector)
    except ValueError as exc:
        return {"ok": False, "message": f"Ollama Embedding 响应无效：{exc}"}

    return {
        "ok": True,
        "message": f"连接正常，Embedding 模型已验证（维度 {dim}）",
    }


async def _probe_ollama_legacy_embedding(
    *,
    base_url: str,
    model: str,
    timeout: float,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/api/embeddings"
    body = {"model": model, "prompt": _EMBEDDING_PROBE_TEXT}
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(url, json=body)
    except httpx.HTTPError as exc:
        return _safe_failure("无法连接 Ollama Embedding 服务", exc)
    if response.status_code >= 400:
        return {
            "ok": False,
            "message": f"Ollama Embedding 返回 HTTP {response.status_code}",
        }
    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "message": "Ollama Embedding 返回的不是 JSON"}
    try:
        vector = _extract_ollama_embedding(data)
        dim = _validate_embedding_vector(vector)
    except ValueError as exc:
        return {"ok": False, "message": f"Ollama Embedding 响应无效：{exc}"}
    return {
        "ok": True,
        "message": f"连接正常，Embedding 模型已验证（维度 {dim}）",
    }


# --- Model list endpoint ---------------------------------------------------


async def _request_model_ids(
    *,
    url: str,
    timeout: float,
    list_key: str,
    headers: dict[str, str] | None = None,
) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "connection_failed",
                "message": f"无法连接模型服务：{type(exc).__name__}",
            },
        ) from None

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "upstream_error",
                "message": f"模型服务返回 HTTP {response.status_code}",
            },
        )
    try:
        payload = response.json()
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "invalid_json",
                "message": "模型服务返回的不是模型列表，请检查已保存的 API 地址",
            },
        ) from None
    try:
        return _extract_model_ids(payload, list_key=list_key)
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "invalid_format",
                "message": "模型服务返回的模型列表格式不正确",
            },
        ) from None


@router.get("/models", response_model=dict[str, Any])
async def list_models(
    db: AsyncSession = Depends(get_db),
    admin: Admin = Depends(require_admin),
) -> dict[str, Any]:
    """List models using only the encrypted, persisted provider configuration."""

    row = await _get_or_create_config(db)
    timeout = float(row.timeout_seconds or 30)
    if row.provider == "openai":
        if not row.openai_base_url:
            raise HTTPException(
                status_code=400,
                detail={"code": "missing_base_url", "message": "请先保存 OpenAI 兼容服务地址"},
            )
        if not row.has_api_key or not row.openai_api_key_cipher:
            raise HTTPException(
                status_code=400,
                detail={"code": "missing_api_key", "message": "请先保存 OpenAI 兼容密钥"},
            )
        try:
            api_key = decrypt_secret(row.openai_api_key_cipher)
        except (SecretStoreError, SecretDecryptError):
            logger.warning("ai_models_decrypt_failed admin=%s", admin.username)
            raise HTTPException(
                status_code=500,
                detail={"code": "decrypt_failed", "message": "已保存的密钥无法读取，请重新保存"},
            ) from None
        if not api_key:
            raise HTTPException(
                status_code=400,
                detail={"code": "missing_api_key", "message": "请先保存 OpenAI 兼容密钥"},
            )
        model_ids = await _request_model_ids(
            url=f"{row.openai_base_url.rstrip('/')}/models",
            timeout=timeout,
            list_key="data",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
        current_model = row.openai_model or ""
    elif row.provider == "ollama":
        if not row.ollama_base_url:
            raise HTTPException(
                status_code=400,
                detail={"code": "missing_base_url", "message": "请先保存 Ollama 服务地址"},
            )
        model_ids = await _request_model_ids(
            url=f"{row.ollama_base_url.rstrip('/')}/api/tags",
            timeout=timeout,
            list_key="models",
        )
        current_model = row.ollama_model or ""
    else:
        raise HTTPException(
            status_code=400,
            detail={"code": "provider_disabled", "message": "当前未启用任何模型来源"},
        )

    sorted_ids = sorted(set(model_ids), key=_natural_model_key)[:500]
    return {
        "models": [{"id": model_id} for model_id in sorted_ids],
        "provider": row.provider,
        "current_model": current_model.strip(),
    }
