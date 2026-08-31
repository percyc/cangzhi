"""Build an external OCR provider from a persisted AI runtime row.

The factory deliberately only depends on the :class:`AIRuntimeConfig`
row, not on the ``apps.api`` package's secret store, so callers in
the worker (sync) and the API (async) can share the same code.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models.auth import AIRuntimeConfig
from ..security.secrets import SecretDecryptError, SecretStoreError, decrypt_secret
from .base import ExternalOcrProvider
from .openai_compatible import OpenAICompatibleOcrProvider

logger = logging.getLogger(__name__)


def build_external_ocr_provider(
    row: AIRuntimeConfig | None,
) -> ExternalOcrProvider | None:
    """Return a configured provider for the row, or ``None`` if disabled.

    The row is *not* mutated; the API key is decrypted on the fly
    and the result lives only on the returned provider instance.
    Decryption failures are logged with the operator username (not
    the key) and treated as "not configured" so a stale cipher
    cannot silently downgrade the OCR channel.
    """

    if row is None:
        return None
    provider_name = (row.ocr_provider or "disabled").strip().lower()
    if provider_name == "disabled" or not provider_name:
        return None
    if provider_name == "openai":
        return _build_openai_provider(row)
    logger.warning(
        "external_ocr_unknown_provider provider=%s", provider_name
    )
    return None


def _build_openai_provider(
    row: AIRuntimeConfig,
) -> ExternalOcrProvider | None:
    if not row.ocr_base_url or not row.ocr_model:
        return None
    if not row.has_ocr_api_key or not row.ocr_api_key_cipher:
        return None
    try:
        api_key = decrypt_secret(row.ocr_api_key_cipher)
    except (SecretStoreError, SecretDecryptError) as exc:
        logger.warning(
            "external_ocr_decrypt_failed category=%s", type(exc).__name__
        )
        return None
    if not api_key:
        return None
    timeout = float(row.ocr_timeout_seconds or 30)
    return OpenAICompatibleOcrProvider(
        base_url=row.ocr_base_url,
        api_key=api_key,
        model=row.ocr_model,
        timeout=timeout,
    )


def describe_provider(provider: ExternalOcrProvider | None) -> dict[str, Any]:
    """Return a small, secret-free description of the provider.

    Used by the worker summary log and by the processing-status
    API so the front-end can show "外部 OCR 触发 X 页" without
    exposing any key. The dict is JSON-safe and contains only
    strings.
    """

    if provider is None:
        return {"provider": "", "model": ""}
    info = provider.describe() or {}
    return {
        "provider": str(info.get("provider") or provider.name or ""),
        "model": str(info.get("model") or ""),
    }
