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
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..models.auth import Admin, AIRuntimeConfig
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
    if not cleaned or len(cleaned) > 200:
        raise ValueError("模型名称长度应在 1-200 之间")
    return cleaned


class _OpenAIConfig(BaseModel):
    base_url: str = Field(min_length=1, max_length=512)
    model: str = Field(min_length=1, max_length=255)

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
    model: str = Field(min_length=1, max_length=255)

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return _validate_url(value)

    @field_validator("model")
    @classmethod
    def _model_name(cls, value: str) -> str:
        return _validate_model(value)


class AIConfigUpdate(BaseModel):
    provider: str = Field(min_length=1, max_length=16)
    openai: _OpenAIConfig | None = None
    ollama: _OllamaConfig | None = None
    api_key_action: str = Field(default="keep", max_length=16)
    api_key: str | None = Field(default=None, max_length=512)
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    prompt_version: str | None = Field(default=None, max_length=64)

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
    if payload.prompt_version:
        row.prompt_version = payload.prompt_version.strip()[:64] or "v1"

    if payload.provider == "openai":
        if payload.openai is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "openai_missing", "message": "请填写 OpenAI-compatible 配置"},
            )
        row.openai_base_url = payload.openai.base_url
        row.openai_model = payload.openai.model
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
        row.ollama_model = payload.ollama.model
    row.updated_by = admin.id
    db.add(row)
    await db.commit()
    await db.refresh(row)
    logger.info(
        "ai_runtime_updated provider=%s admin=%s key_set=%s",
        row.provider,
        admin.username,
        row.has_api_key,
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
    return {"ok": True, "message": "连接正常"}


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
    if response.status_code >= 400:
        return {
            "ok": False,
            "message": f"Ollama 返回 HTTP {response.status_code}，请检查服务地址",
        }
    return {"ok": True, "message": "连接正常"}


def _safe_failure(prefix: str, exc: Exception) -> dict[str, Any]:
    return {"ok": False, "message": f"{prefix}：{type(exc).__name__}"}
