"""Embedding configuration compatibility test endpoints (ADR-015).

The router exposes two operations:

* ``POST /api/embeddings/test`` runs the canary probe against the
  embedding channel saved in :class:`AIRuntimeConfig`, compares
  the result to the active :class:`EmbeddingProfile` (if any) and
  returns a JSON-safe verdict plus the per-canary cosine scores.
  The endpoint never returns the API key, the ciphertext, or the
  raw vectors.

* ``GET /api/embeddings/status`` returns the active profile and
  the most recent tested profile, also JSON-safe. It is the
  read-only companion used by the front-end to render "what index
  is currently active?" and "what is the operator's last tested
  candidate?".

The router is intentionally narrow. It does not own any state
machine transitions, build, or activation — those live in the
service layer and are out of scope for M3-6b phase 1.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..embeddings import CANARY_VERSION
from ..embeddings.service import test_candidate_embedding
from ..models.auth import AIRuntimeConfig
from ..models.embedding_profiles import EmbeddingProfile
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
    """Return the current embedding configuration and profile state.

    The endpoint is read-only and never probes the upstream. It
    answers three questions:

    * Is there an active profile, and if so, which one?
    * What is the most recent tested profile (may equal the
      active one)?
    * What does the operator's saved ``ai_runtime_configs`` row
      look like in terms of the embedding channel?
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

    return {
        "canary_version": CANARY_VERSION,
        "config": config_summary,
        "active_profile": _active_profile_summary(active_profile),
        "last_tested": _active_profile_summary(last_tested),
    }
