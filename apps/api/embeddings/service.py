"""Service layer for the embedding compatibility test (ADR-015).

This module is the *only* place that performs I/O for the
embedding probe. The pure helpers in :mod:`apps.api.embeddings`
(compatibility, fingerprint, canary) are deliberately I/O-free so
they can be unit-tested in isolation; the service is what glues
them to the database and the embedding endpoint.

Responsibilities:

* Read the saved embedding configuration from
  :class:`AIRuntimeConfig`.
* Call the configured embedding endpoint with the fixed canary
  texts and validate the response.
* Look up the active profile, if any, and run the compatibility
  judgement.
* Persist (or update) a profile row in the ``tested`` state.
* Return a JSON-safe summary the API layer can hand to the
  operator. The summary must never contain a key, a ciphertext,
  or a raw vector.

The module does not yet build, activate, or roll back any index.
That is the M3-6b phase 2 / M3-6c work. This milestone only
records the operator's candidate configuration, runs the canary
test, and stamps the result on the profile row so the next phase
can pick it up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.auth import AIRuntimeConfig
from ..models.embedding_profiles import EmbeddingProfile
from ..security.secrets import (
    SecretDecryptError,
    SecretStoreError,
    decrypt_secret,
    get_secret_store,
)
from .canary import CANARY_VERSION, get_canary_texts
from .compatibility import (
    EXPECTED_CANARY_COUNT,
    CompatibilityDecision,
    judge_compatibility,
    validate_embedding_vector,
)
from .fingerprint import compute_config_fingerprint


logger = logging.getLogger(__name__)


# The single-row ``ai_runtime_configs`` table uses a UNIQUE
# constraint on ``singleton_key`` to enforce "at most one row". The
# service uses the same trick as :mod:`apps.api.api.settings_ai` to
# create the row on first read: insert with the default key, fall
# back to a SELECT if the insert races with another request.
_AI_SINGLETON_KEY = "singleton"


# ``client_factory`` lets tests inject an :class:`httpx.AsyncClient`
# substitute. In production we use the real one with
# ``trust_env=False`` so proxy variables do not leak into probes.
ClientFactory = Callable[..., httpx.AsyncClient]


@dataclass(frozen=True)
class CandidateDescriptor:
    """The fields we need to fingerprint a candidate.

    ``api_key`` is the decrypted bearer token. It is only ever
    held in memory long enough to send the probe request; it is
    never logged, never stored, never returned to the API layer.
    The ``key_fingerprint`` field is what the persistence layer
    records — it identifies the master Fernet key, not the API
    key, and rotating the API key therefore does not change it.
    """

    provider: str
    base_url: str
    model: str
    has_api_key: bool
    key_fingerprint: str | None
    api_key: str
    timeout_seconds: float


@dataclass(frozen=True)
class TestResult:
    """The result the service hands back to the API layer.

    The dataclass is the only object that crosses the API
    boundary; the dict it eventually becomes must contain neither
    the canary vectors nor any key material.
    """

    ok: bool
    decision: CompatibilityDecision
    reason: str
    config_fingerprint: str
    provider: str
    base_url: str
    model: str
    dim: int
    has_api_key: bool
    key_fingerprint: str | None
    canary_count: int
    scores: list[float]
    average: float
    threshold: float
    profile_id: int | None
    profile_status: str | None
    active_profile_id: int | None
    active_fingerprint: str | None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "decision": self.decision.value,
            "reason": self.reason,
            "config": {
                "provider": self.provider,
                "base_url": self.base_url,
                "model": self.model,
                "dim": self.dim,
                "has_api_key": self.has_api_key,
                "key_fingerprint": self.key_fingerprint,
                "config_fingerprint": self.config_fingerprint,
                "canary_count": self.canary_count,
                "canary_version": CANARY_VERSION,
            },
            "scores": list(self.scores),
            "average": self.average,
            "threshold": self.threshold,
            "profile": {
                "id": self.profile_id,
                "status": self.profile_status,
            },
            "active": {
                "id": self.active_profile_id,
                "fingerprint": self.active_fingerprint,
            },
        }


# --- Failure helpers -----------------------------------------------------


def _failure(
    *,
    reason: str,
    config_fingerprint: str = "",
    provider: str = "",
    base_url: str = "",
    model: str = "",
    dim: int = 0,
    has_api_key: bool = False,
    key_fingerprint: str | None = None,
    active_profile_id: int | None = None,
    active_fingerprint: str | None = None,
    scores: list[float] | None = None,
    average: float = 0.0,
    threshold: float = 0.0,
) -> TestResult:
    return TestResult(
        ok=False,
        decision=CompatibilityDecision.REBUILD_REQUIRED,
        reason=reason,
        config_fingerprint=config_fingerprint,
        provider=provider,
        base_url=base_url,
        model=model,
        dim=dim,
        has_api_key=has_api_key,
        key_fingerprint=key_fingerprint,
        canary_count=0,
        scores=list(scores or []),
        average=average,
        threshold=threshold,
        profile_id=None,
        profile_status=None,
        active_profile_id=active_profile_id,
        active_fingerprint=active_fingerprint,
    )


# --- Configuration resolution --------------------------------------------


async def _ensure_config_row(db: AsyncSession) -> AIRuntimeConfig:
    """Materialise the singleton ``ai_runtime_configs`` row.

    The row carries the same defaults as
    :func:`apps.api.api.settings_ai._get_or_create_config`. We
    re-implement the helper here instead of importing it to keep
    the embeddings package independent of the settings router.
    """

    row = (
        await db.execute(
            select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc()).limit(1)
        )
    ).scalars().first()
    if row is not None:
        return row
    new_row = AIRuntimeConfig(
        provider="disabled",
        timeout_seconds=30,
        prompt_version="v1",
        singleton_key=_AI_SINGLETON_KEY,
    )
    db.add(new_row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = (
            await db.execute(
                select(AIRuntimeConfig)
                .order_by(AIRuntimeConfig.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if existing is None:
            raise
        return existing
    await db.refresh(new_row)
    return new_row


async def _resolve_candidate(
    db: AsyncSession, *, decrypt_api_key: bool = True
) -> tuple[CandidateDescriptor | None, str | None]:
    """Read the active ``ai_runtime_configs`` row.

    Returns either a :class:`CandidateDescriptor` or a Chinese
    error message describing why the operator's saved configuration
    cannot be probed. The function never raises — the API layer
    relies on the ``(None, reason)`` contract to surface a gentle
    error without a 5xx.

    The ``decrypt_api_key`` flag lets unit tests avoid the master
    key setup. Production code leaves it on.

    The first call to this function materialises the singleton
    ``ai_runtime_configs`` row if it does not yet exist; the
    row carries the same defaults as
    :func:`apps.api.api.settings_ai._get_or_create_config` so the
    error message ("尚未选择 Embedding 模型") matches the rest of
    the settings page.
    """

    await _ensure_config_row(db)
    row = (
        await db.execute(
            select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc()).limit(1)
        )
    ).scalars().first()
    if row is None:
        return None, "尚未配置模型，请先在设置中启用"

    if not row.embedding_model or not row.embedding_model.strip():
        return None, "尚未选择 Embedding 模型"
    if row.embedding_provider == "disabled":
        return None, "Embedding 渠道未启用"
    if row.embedding_provider not in {"openai", "ollama"}:
        return None, "不支持的 Embedding 模型来源"
    if not row.embedding_base_url or not row.embedding_base_url.strip():
        return None, "尚未配置 Embedding 服务地址"

    has_api_key = bool(row.has_embedding_api_key and row.embedding_api_key_cipher)
    if row.embedding_provider == "openai" and not has_api_key:
        return None, "尚未保存 Embedding 服务的 OpenAI 兼容密钥"

    api_key = ""
    if has_api_key and row.embedding_api_key_cipher and decrypt_api_key:
        try:
            api_key = decrypt_secret(row.embedding_api_key_cipher)
        except (SecretStoreError, SecretDecryptError):
            return None, "主密钥不匹配或密文已损坏，请联系管理员"

    # ``key_fingerprint`` is derived from the *master* Fernet key,
    # not from the API key, so rotating the API key does not
    # change it. The fingerprint's purpose is to detect a
    # configuration that was tested under a different master key,
    # which is an unsupported deployment.
    key_fp: str | None = None
    if has_api_key and row.embedding_api_key_cipher:
        try:
            key_fp = get_secret_store().key_fingerprint()
        except (SecretStoreError, SecretDecryptError):
            key_fp = None

    descriptor = CandidateDescriptor(
        provider=row.embedding_provider,
        base_url=row.embedding_base_url.strip().rstrip("/"),
        model=row.embedding_model.strip(),
        has_api_key=has_api_key,
        key_fingerprint=key_fp,
        api_key=api_key,
        timeout_seconds=float(row.embedding_timeout_seconds or 30),
    )
    return descriptor, None


# --- Embedding probe -----------------------------------------------------


def _extract_openai_canary(payload: Any) -> list[list[float]]:
    """Parse a batched OpenAI /v1/embeddings response.

    The response shape is documented as
    ``{"data": [{"embedding": [...]}, ...]}`` with the order
    matching the input. We only return the first three rows; the
    caller asserts the count.
    """

    if not isinstance(payload, dict):
        raise ValueError("top-level response is not an object")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("响应缺少 data 字段")
    vectors: list[list[float]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"响应 data[{index}] 不是对象")
        vector = item.get("embedding")
        validate_embedding_vector(vector)
        vectors.append([float(x) for x in vector])
    return vectors


def _extract_ollama_canary(payload: Any) -> list[list[float]]:
    """Parse a batched Ollama /api/embed response.

    The new ``/api/embed`` endpoint returns
    ``{"embeddings": [[...], [...]]}``; the legacy
    ``/api/embeddings`` returns ``{"embedding": [...]}`` and we
    degrade gracefully to it.
    """

    if not isinstance(payload, dict):
        raise ValueError("top-level response is not an object")
    embeddings = payload.get("embeddings")
    if isinstance(embeddings, list) and embeddings:
        vectors: list[list[float]] = []
        for index, vector in enumerate(embeddings):
            if not isinstance(vector, list) or not vector:
                raise ValueError(f"响应 embeddings[{index}] 不是数组")
            validate_embedding_vector(vector)
            vectors.append([float(x) for x in vector])
        return vectors
    legacy = payload.get("embedding")
    if isinstance(legacy, list) and legacy:
        validate_embedding_vector(legacy)
        return [[float(x) for x in legacy]]
    raise ValueError("响应缺少 embeddings/embedding 字段")


async def _request_canary_vectors(
    *,
    descriptor: CandidateDescriptor,
    texts: Sequence[str],
    client_factory: ClientFactory | None = None,
) -> list[list[float]]:
    """Send the canary texts to the configured embedding endpoint.

    The factory parameter exists so tests can inject a mock
    transport. Production code passes ``None`` and gets the real
    ``httpx.AsyncClient`` with ``trust_env=False``.
    """

    factory = client_factory or (
        lambda **kwargs: httpx.AsyncClient(**kwargs)
    )
    headers = {"Accept": "application/json"}
    if descriptor.provider == "openai":
        url = f"{descriptor.base_url.rstrip('/')}/embeddings"
        if not descriptor.api_key:
            raise ValueError("Embedding 密钥为空，无法发起探针")
        headers["Authorization"] = f"Bearer {descriptor.api_key}"
        headers["Content-Type"] = "application/json"
        body = {"model": descriptor.model, "input": list(texts)}
    else:
        url = f"{descriptor.base_url.rstrip('/')}/api/embed"
        headers["Content-Type"] = "application/json"
        body = {"model": descriptor.model, "input": list(texts)}

    try:
        async with factory(timeout=descriptor.timeout_seconds, trust_env=False) as client:
            response = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        # Sanitise: never echo the underlying message because it
        # sometimes contains a URL with embedded credentials.
        raise ValueError(f"无法连接 Embedding 服务：{type(exc).__name__}") from exc

    if response.status_code >= 400:
        # Legacy Ollama fallback handled in the caller so the
        # verdict message is more useful.
        raise ValueError(f"Embedding 服务返回 HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("Embedding 服务返回的不是 JSON") from exc

    if descriptor.provider == "openai":
        return _extract_openai_canary(payload)
    return _extract_ollama_canary(payload)


async def _request_canary_vectors_ollama_with_legacy_fallback(
    *,
    descriptor: CandidateDescriptor,
    texts: Sequence[str],
    client_factory: ClientFactory | None = None,
) -> list[list[float]]:
    """Like :func:`_request_canary_vectors` but tries the legacy
    ``/api/embeddings`` endpoint on 404/405 for older Ollama builds.

    The legacy endpoint only accepts a single ``prompt`` per call,
    so the fallback iterates the canary texts sequentially. The
    fallback exists only to keep the test endpoint usable for
    operators who still run an older Ollama; it is not a path
    used by the production probe in M3-6b phase 1.
    """

    try:
        return await _request_canary_vectors(
            descriptor=descriptor,
            texts=texts,
            client_factory=client_factory,
        )
    except ValueError as exc:
        if descriptor.provider != "ollama":
            raise
        if "HTTP 404" not in str(exc) and "HTTP 405" not in str(exc):
            raise
        factory = client_factory or (
            lambda **kwargs: httpx.AsyncClient(**kwargs)
        )
        url = f"{descriptor.base_url.rstrip('/')}/api/embeddings"
        vectors: list[list[float]] = []
        try:
            async with factory(
                timeout=descriptor.timeout_seconds, trust_env=False
            ) as client:
                for text in texts:
                    response = await client.post(
                        url,
                        json={"model": descriptor.model, "prompt": text},
                    )
                    if response.status_code >= 400:
                        raise ValueError(
                            f"Ollama Embedding 返回 HTTP {response.status_code}"
                        )
                    try:
                        payload = response.json()
                    except ValueError as inner_exc:
                        raise ValueError(
                            "Ollama Embedding 返回的不是 JSON"
                        ) from inner_exc
                    vectors.append(_extract_ollama_canary(payload)[0])
        except httpx.HTTPError as inner_exc:
            raise ValueError(
                f"无法连接 Embedding 服务：{type(inner_exc).__name__}"
            ) from inner_exc
        return vectors


# --- Persistence ---------------------------------------------------------


async def _upsert_tested_profile(
    db: AsyncSession,
    *,
    descriptor: CandidateDescriptor,
    dim: int,
    canary_vectors: list[list[float]],
    config_fingerprint: str,
    decision: CompatibilityDecision,
    reason: str,
) -> EmbeddingProfile:
    """Insert or update a ``tested`` profile row.

    The function is intentionally idempotent on
    ``config_fingerprint``: re-testing the exact same
    configuration overwrites the previous row's
    ``last_tested_at`` and ``canary_vectors`` rather than creating
    duplicates. The status always lands on ``tested``; the
    activation flag lives on
    :class:`AIRuntimeConfig.active_embedding_profile_id`.
    """

    existing = (
        await db.execute(
            select(EmbeddingProfile).where(
                EmbeddingProfile.config_fingerprint == config_fingerprint
            )
        )
    ).scalars().first()

    now = datetime.now(tz=timezone.utc)
    if existing is None:
        profile = EmbeddingProfile(
            provider=descriptor.provider,
            base_url=descriptor.base_url,
            model=descriptor.model,
            dim=dim,
            has_api_key=descriptor.has_api_key,
            key_fingerprint=descriptor.key_fingerprint,
            config_fingerprint=config_fingerprint,
            canary_vectors=list(canary_vectors),
            status="tested",
            last_tested_at=now,
            last_error=None,
        )
        db.add(profile)
    else:
        existing.provider = descriptor.provider
        existing.base_url = descriptor.base_url
        existing.model = descriptor.model
        existing.dim = dim
        existing.has_api_key = descriptor.has_api_key
        existing.key_fingerprint = descriptor.key_fingerprint
        existing.canary_vectors = list(canary_vectors)
        existing.status = "tested"
        existing.last_tested_at = now
        existing.last_error = None
        profile = existing

    await db.commit()
    await db.refresh(profile)
    logger.info(
        "embedding_profile_tested id=%s fingerprint=%s decision=%s reason=%s",
        profile.id,
        config_fingerprint,
        decision.value,
        reason,
    )
    return profile


# --- Public entry point --------------------------------------------------


async def test_candidate_embedding(
    db: AsyncSession,
    *,
    client_factory: ClientFactory | None = None,
) -> TestResult:
    """Run the ADR-015 compatibility test for the current operator config.

    The function reads the saved :class:`AIRuntimeConfig` row,
    sends the canary texts to the configured embedding endpoint,
    compares the result against the active profile (if any), and
    persists a ``tested`` profile row.

    The returned :class:`TestResult` is what the API layer turns
    into a JSON response. The result never contains the key, the
    ciphertext, or the canary vectors — only the per-canary cosine
    scores.
    """

    descriptor, error_reason = await _resolve_candidate(db)
    active_row = await _load_active_state(db)
    if descriptor is None:
        return _failure(
            reason=error_reason or "候选配置不可用",
            active_profile_id=active_row["id"],
            active_fingerprint=active_row["fingerprint"],
        )

    canary = get_canary_texts()
    try:
        vectors = await _request_canary_vectors_ollama_with_legacy_fallback(
            descriptor=descriptor,
            texts=canary.texts,
            client_factory=client_factory,
        )
    except ValueError as exc:
        await _record_failure(
            db,
            descriptor=descriptor,
            canary_version=canary.version,
            reason=str(exc),
        )
        return _failure(
            reason=str(exc),
            provider=descriptor.provider,
            base_url=descriptor.base_url,
            model=descriptor.model,
            has_api_key=descriptor.has_api_key,
            key_fingerprint=descriptor.key_fingerprint,
            active_profile_id=active_row["id"],
            active_fingerprint=active_row["fingerprint"],
        )

    if len(vectors) < EXPECTED_CANARY_COUNT:
        reason = (
            f"Embedding 响应向量数量不足"
            f"（{len(vectors)}/{EXPECTED_CANARY_COUNT}）"
        )
        await _record_failure(
            db,
            descriptor=descriptor,
            canary_version=canary.version,
            reason=reason,
        )
        return _failure(
            reason=reason,
            provider=descriptor.provider,
            base_url=descriptor.base_url,
            model=descriptor.model,
            has_api_key=descriptor.has_api_key,
            key_fingerprint=descriptor.key_fingerprint,
            active_profile_id=active_row["id"],
            active_fingerprint=active_row["fingerprint"],
        )

    first_dim = validate_embedding_vector(vectors[0])
    for index, vector in enumerate(vectors[1:], start=1):
        if len(vector) != first_dim:
            reason = (
                f"第 {index} 条探针返回维度 {len(vector)} 与第一条 {first_dim} 不一致"
            )
            await _record_failure(
                db,
                descriptor=descriptor,
                canary_version=canary.version,
                reason=reason,
            )
            return _failure(
                reason=reason,
                provider=descriptor.provider,
                base_url=descriptor.base_url,
                model=descriptor.model,
                dim=first_dim,
                has_api_key=descriptor.has_api_key,
                key_fingerprint=descriptor.key_fingerprint,
                active_profile_id=active_row["id"],
                active_fingerprint=active_row["fingerprint"],
            )

    config_fingerprint = compute_config_fingerprint(
        provider=descriptor.provider,
        base_url=descriptor.base_url,
        model=descriptor.model,
        dim=first_dim,
        has_api_key=descriptor.has_api_key,
        key_fingerprint=descriptor.key_fingerprint,
        canary_version=canary.version,
    )

    decision, reason, diagnostics = judge_compatibility(
        candidate_dim=first_dim,
        candidate_model=descriptor.model,
        candidate_fingerprint=config_fingerprint,
        candidate_canary_vectors=vectors,
        active_dim=active_row["dim"],
        active_model=active_row["model"],
        active_fingerprint=active_row["fingerprint"],
        active_canary_vectors=active_row["canary_vectors"],
    )

    profile = await _upsert_tested_profile(
        db,
        descriptor=descriptor,
        dim=first_dim,
        canary_vectors=vectors,
        config_fingerprint=config_fingerprint,
        decision=decision,
        reason=reason,
    )

    return TestResult(
        ok=True,
        decision=decision,
        reason=reason,
        config_fingerprint=config_fingerprint,
        provider=descriptor.provider,
        base_url=descriptor.base_url,
        model=descriptor.model,
        dim=first_dim,
        has_api_key=descriptor.has_api_key,
        key_fingerprint=descriptor.key_fingerprint,
        canary_count=len(vectors),
        scores=list(diagnostics.get("scores", [])),
        average=float(diagnostics.get("average", 0.0)),
        threshold=float(diagnostics.get("threshold", 0.0)),
        profile_id=profile.id,
        profile_status=profile.status,
        active_profile_id=active_row["id"],
        active_fingerprint=active_row["fingerprint"],
    )


# --- Helpers -------------------------------------------------------------


async def _load_active_state(db: AsyncSession) -> dict:
    """Read the active profile's descriptor from the database.

    The result is a plain dict so the rest of the service code
    stays declarative. ``None`` fields mean "no active profile" and
    trigger the ``unknown`` decision.
    """

    row = (
        await db.execute(
            select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc()).limit(1)
        )
    ).scalars().first()
    if row is None or row.active_embedding_profile_id is None:
        return {
            "id": None,
            "fingerprint": None,
            "model": None,
            "dim": None,
            "canary_vectors": None,
        }
    profile = (
        await db.execute(
            select(EmbeddingProfile).where(
                EmbeddingProfile.id == row.active_embedding_profile_id
            )
        )
    ).scalars().first()
    if profile is None:
        return {
            "id": None,
            "fingerprint": None,
            "model": None,
            "dim": None,
            "canary_vectors": None,
        }
    return {
        "id": profile.id,
        "fingerprint": profile.config_fingerprint,
        "model": profile.model,
        "dim": profile.dim,
        "canary_vectors": list(profile.canary_vectors or []),
    }


async def _record_failure(
    db: AsyncSession,
    *,
    descriptor: CandidateDescriptor,
    canary_version: str,
    reason: str,
) -> None:
    """Best-effort: stamp the failure on a ``failed`` profile row.

    A failure during the probe does not delete the previous tested
    profile. We just record the sanitised error message and leave
    the existing index alone. The placeholder ``dim=1`` is
    required by the ``dim > 0`` CHECK constraint; the ``last_error``
    column flags the real outcome.
    """

    fingerprint = compute_config_fingerprint(
        provider=descriptor.provider,
        base_url=descriptor.base_url,
        model=descriptor.model,
        dim=1,  # placeholder; the real dim is unknown on failure
        has_api_key=descriptor.has_api_key,
        key_fingerprint=descriptor.key_fingerprint,
        canary_version=canary_version,
    )
    existing = (
        await db.execute(
            select(EmbeddingProfile).where(
                EmbeddingProfile.config_fingerprint == fingerprint
            )
        )
    ).scalars().first()
    now = datetime.now(tz=timezone.utc)
    if existing is None:
        existing = EmbeddingProfile(
            provider=descriptor.provider,
            base_url=descriptor.base_url,
            model=descriptor.model,
            dim=1,
            has_api_key=descriptor.has_api_key,
            key_fingerprint=descriptor.key_fingerprint,
            config_fingerprint=fingerprint,
            canary_vectors=None,
            status="failed",
            last_tested_at=now,
            last_error=reason,
        )
        db.add(existing)
    else:
        existing.status = "failed"
        existing.last_tested_at = now
        existing.last_error = reason
    await db.commit()
