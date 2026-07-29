"""Embedding endpoints: test, status, build, retry, activate, rollback.

The router bundles the M3-6b phase 1 endpoints (``/test`` and
``/status``) together with the phase 2 lifecycle endpoints
(``/profiles/{id}/build``, ``/profiles/{id}/retry``,
``/profiles/{id}/activate`` and ``/profiles/{id}/rollback``).
The two halves share the same prefix, the same admin gate and
the same JSON hygiene (no keys, no vectors, no canary data) so
the front-end only has to know one URL family.

The lifecycle endpoints are deliberately thin: every state
transition and database lock is implemented in
:mod:`apps.api.embeddings.build_service`. The router's job is
limited to translating HTTP into the service call and turning
the service's ``BuildServiceError`` subclasses into the right
HTTP status. The rule of thumb is: any business rule that
would make sense to unit-test belongs in the service, not the
router.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..embeddings import CANARY_VERSION
from ..embeddings.build_service import (
    ACTIVATABLE_STATUSES,
    BUILDABLE_STATUSES,
    RETRYABLE_STATUSES,
    BuildServiceError,
    activate_profile,
    get_profile_summaries,
    retry_profile,
    rollback_profile,
    start_build,
)
from ..embeddings.service import test_candidate_embedding
from ..models.auth import AIRuntimeConfig
from ..models.embedding_profiles import ChunkEmbedding, EmbeddingProfile
from ..models.processing import ProcessingJob
from .auth import require_admin

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/embeddings", tags=["embeddings"])


def _active_profile_summary(profile: EmbeddingProfile | None) -> dict[str, Any]:
    if profile is None:
        return {
            "id": None,
            "status": None,
            "config_fingerprint": None,
            "model": None,
            "dim": None,
            "provider": None,
            "base_url": None,
            "key_fingerprint": None,
        }
    return {
        "id": profile.id,
        "status": profile.status,
        "config_fingerprint": profile.config_fingerprint,
        "model": profile.model,
        "dim": profile.dim,
        "provider": profile.provider,
        "base_url": profile.base_url,
        "key_fingerprint": profile.key_fingerprint,
        "has_api_key": bool(profile.has_api_key),
        "last_tested_at": (
            profile.last_tested_at.isoformat()
            if profile.last_tested_at
            else None
        ),
    }


@router.post("/test", response_model=dict[str, Any])
async def test_embedding_configuration(
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
) -> dict[str, Any]:
    """Run the compatibility probe and return the verdict."""

    result = await test_candidate_embedding(db)
    return result.to_public_dict()


@router.get("/status", response_model=dict[str, Any])
async def embedding_status(
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
) -> dict[str, Any]:
    """Return the embedding configuration and the full profile list.

    The endpoint answers three questions for the front-end:

    * What is the operator's current ``ai_runtime_configs`` row?
    * Which profile is the active one (``active_embedding_profile_id``)?
    * What does every profile in the system look like right now
      (status, progress counters, and the set of POST endpoints
      the operator can call next)?
    """

    config_row = (
        await db.execute(
            select(AIRuntimeConfig).order_by(AIRuntimeConfig.id.desc()).limit(1)
        )
    ).scalars().first()
    active_profile: EmbeddingProfile | None = None
    if config_row is not None and config_row.active_embedding_profile_id is not None:
        active_profile = (
            await db.execute(
                select(EmbeddingProfile).where(
                    EmbeddingProfile.id == config_row.active_embedding_profile_id
                )
            )
        ).scalars().first()

    last_tested: EmbeddingProfile | None = (
        await db.execute(
            select(EmbeddingProfile)
            .where(EmbeddingProfile.last_tested_at.is_not(None))
            .order_by(EmbeddingProfile.last_tested_at.desc())
            .limit(1)
        )
    ).scalars().first()

    if config_row is None:
        config_summary = {
            "provider": "disabled",
            "base_url": None,
            "model": None,
            "has_api_key": False,
            "timeout_seconds": 30,
        }
    else:
        config_summary = {
            "provider": config_row.embedding_provider,
            "base_url": config_row.embedding_base_url,
            "model": config_row.embedding_model,
            "has_api_key": bool(config_row.has_embedding_api_key),
            "timeout_seconds": config_row.embedding_timeout_seconds,
        }

    profiles = [summary.to_public_dict() for summary in await get_profile_summaries(db)]
    return {
        "canary_version": CANARY_VERSION,
        "config": config_summary,
        "active_profile": _active_profile_summary(active_profile),
        "last_tested": _active_profile_summary(last_tested),
        "profiles": profiles,
        "status_legend": {
            "buildable": list(BUILDABLE_STATUSES),
            "retryable": list(RETRYABLE_STATUSES),
            "activatable": list(ACTIVATABLE_STATUSES),
        },
    }


# --- /profiles/{id}/... ---------------------------------------------------


def _translate_build_error(exc: BuildServiceError) -> HTTPException:
    """Turn a :class:`BuildServiceError` into a structured 4xx response."""

    return HTTPException(
        status_code=exc.http_status,
        detail={"code": exc.code, "message": exc.message},
    )


@router.post(
    "/profiles/{profile_id}/build",
    response_model=dict[str, Any],
)
async def post_build_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
) -> dict[str, Any]:
    """Start a fresh build for ``profile_id``."""

    try:
        result = await start_build(db, profile_id)
    except BuildServiceError as exc:
        raise _translate_build_error(exc) from None
    return result.to_public_dict()


@router.post(
    "/profiles/{profile_id}/retry",
    response_model=dict[str, Any],
)
async def post_retry_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
) -> dict[str, Any]:
    """Reset failed embedding jobs and resume the build."""

    try:
        result = await retry_profile(db, profile_id)
    except BuildServiceError as exc:
        raise _translate_build_error(exc) from None
    return result.to_public_dict()


@router.post(
    "/profiles/{profile_id}/activate",
    response_model=dict[str, Any],
)
async def post_activate_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
) -> dict[str, Any]:
    """Swap ``profile_id`` to ``active`` and demote the previous one."""

    try:
        result = await activate_profile(db, profile_id)
    except BuildServiceError as exc:
        raise _translate_build_error(exc) from None
    return result.to_public_dict()


@router.post(
    "/profiles/{profile_id}/rollback",
    response_model=dict[str, Any],
)
async def post_rollback_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
) -> dict[str, Any]:
    """Restore ``profile_id`` as the active profile."""

    try:
        result = await rollback_profile(db, profile_id)
    except BuildServiceError as exc:
        raise _translate_build_error(exc) from None
    return result.to_public_dict()


@router.delete(
    "/profiles/{profile_id}",
    response_model=dict[str, Any],
)
async def delete_embedding_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    admin=Depends(require_admin),
) -> dict[str, Any]:
    """Delete an inactive, non-building vector index version."""

    profile = await db.get(EmbeddingProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="向量索引版本不存在")
    config = (
        await db.execute(
            select(AIRuntimeConfig).where(
                AIRuntimeConfig.active_embedding_profile_id == profile_id
            )
        )
    ).scalars().first()
    if profile.status == "active" or config is not None:
        raise HTTPException(
            status_code=409,
            detail="当前生效的向量索引不能删除，请先启用或回滚到其他版本",
        )
    if profile.status == "building":
        raise HTTPException(
            status_code=409,
            detail="正在构建的向量索引不能删除，请等待构建结束",
        )
    await db.execute(
        delete(ProcessingJob).where(
            ProcessingJob.embedding_profile_id == profile_id
        )
    )
    await db.execute(
        delete(ChunkEmbedding).where(ChunkEmbedding.profile_id == profile_id)
    )
    await db.delete(profile)
    await db.commit()
    return {"ok": True, "message": "向量索引版本及其向量数据已删除"}
