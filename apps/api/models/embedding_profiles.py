"""Versioned embedding profile and chunk embedding models.

These tables implement the data plane described in ADR-015 (M3-6b
phase 1). They sit alongside :class:`AIRuntimeConfig` without
replacing it: ``ai_runtime_configs`` keeps the *operator's current
candidate* configuration, while ``embedding_profiles`` records every
historical "version of the index" we have ever validated or built.

* :class:`EmbeddingProfile` is the unit of versioning. Two profiles
  with the same ``config_fingerprint`` are guaranteed to live in the
  same vector space; profiles with different fingerprints are not.
* :class:`ChunkEmbedding` is the versioned index itself. Each row
  binds one ``document_chunks`` row to one profile, with the
  embedding vector (and the ``content_hash`` that produced it) so
  re-embedding is idempotent.

PostgreSQL stores the vector as a pgvector ``VECTOR`` (declared with
no fixed dimension because ``dim`` is a runtime value picked by the
operator). SQLite (used by the test suite) stores the same payload
as a JSON array so the test path can exercise the data plane
without a pgvector extension. HNSW indexes are deliberately not
created in this milestone; ADR-015 says we only add them after the
profile is ready *and* the corpus is large enough to benefit.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import JSON
from pgvector.sqlalchemy import Vector

from .base import BaseModel

# PostgreSQL uses JSONB; SQLite keeps the same column as plain JSON
# text. The test suite relies on the JSON variant, production relies
# on JSONB.
_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")

# The vector column uses pgvector on PostgreSQL and JSON everywhere
# else. The chunk_embeddings table only needs to *store* and
# *retrieve* the vector; the SQL-level search helpers will be added
# in M3-6c alongside the HNSW indexes.
_VECTOR_TYPE = Vector().with_variant(JSON(), "sqlite")


# Status values for the embedding profile state machine. The
# transition rules are documented in ADR-015; this list is enforced
# at the database level so even a buggy client cannot accidentally
# store an out-of-state value.
_EMBEDDING_PROFILE_STATUSES = (
    "draft",
    "tested",
    "building",
    "ready",
    "active",
    "retired",
    "failed",
)


class EmbeddingProfile(BaseModel):
    """A versioned snapshot of an embedding pipeline configuration.

    Attributes
    ----------
    provider, base_url, model, dim:
        The four fields that fully determine the vector space a
        profile produces. The fingerprint is computed from them
        together with the API key existence flag, so a profile with
        the same provider/URL/model/dim and a rotated key is still
        considered "the same configuration".
    has_api_key:
        Whether the operator has a key stored in
        ``ai_runtime_configs`` for this profile. The key itself is
        *never* copied into this table.
    key_fingerprint:
        Short, non-reversible identifier derived from the encrypted
        key (see :func:`SecretStore.key_fingerprint`). Used to keep
        the configuration fingerprint stable across key rotations.
    config_fingerprint:
        Deterministic SHA-256-derived hash over the fields above.
        Two profiles with the same ``config_fingerprint`` are
        guaranteed to live in the same vector space.
    canary_vectors:
        JSON-serialised list of three vectors (one per fixed canary
        text). Stored as text so the same schema works for SQLite;
        PostgreSQL users can re-parse this as JSON if they want to
        query against it (the data plane does not need to).
    status:
        One of ``draft``, ``tested``, ``building``, ``ready``,
        ``active``, ``retired``, ``failed``. See ADR-015.
    last_tested_at, last_error:
        Operator-facing audit trail. ``last_error`` must never
        contain key material.
    """

    __tablename__ = "embedding_profiles"

    provider = Column(String(16), nullable=False)
    base_url = Column(String(512), nullable=False)
    model = Column(String(255), nullable=False)
    dim = Column(Integer, nullable=False)
    has_api_key = Column(
        Boolean, nullable=False, server_default=text("false")
    )
    key_fingerprint = Column(String(128), nullable=True)
    config_fingerprint = Column(String(128), nullable=False, index=True)
    # Canary vectors: list[float * dim] * 3. Stored as a JSON string
    # (PostgreSQL JSONB / SQLite text) so the same column works for
    # both databases. We never read these rows from the index path;
    # they are only used by the compatibility test endpoint.
    canary_vectors = Column(_JSON_TYPE, nullable=True)
    status = Column(
        String(16),
        nullable=False,
        server_default=text("'draft'"),
    )
    last_tested_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)

    # --- M3-6b phase 2: build progress (ADR-015 §"构建/激活/回滚")
    # The counters are NULL for profiles that never entered the
    # build pipeline (tested / failed). The constraints in
    # migration 0009 enforce ``all three NULL or all three NOT
    # NULL`` so a buggy client cannot poison the progress view.
    total_chunks = Column(Integer, nullable=True)
    completed_chunks = Column(Integer, nullable=True)
    failed_chunks = Column(Integer, nullable=True)
    build_started_at = Column(DateTime(timezone=True), nullable=True)
    build_finished_at = Column(DateTime(timezone=True), nullable=True)
    activated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "dim > 0",
            name="ck_embedding_profiles_dim_positive",
        ),
        CheckConstraint(
            f"status in {_EMBEDDING_PROFILE_STATUSES}",
            name="ck_embedding_profiles_status",
        ),
        CheckConstraint(
            "provider in ('openai', 'ollama')",
            name="ck_embedding_profiles_provider",
        ),
        CheckConstraint(
            "total_chunks IS NULL OR total_chunks >= 0",
            name="ck_embedding_profiles_counters_nonneg",
        ),
        CheckConstraint(
            "build_started_at IS NULL OR build_finished_at IS NULL "
            "OR build_finished_at >= build_started_at",
            name="ck_embedding_profiles_build_order",
        ),
        Index("ix_embedding_profiles_status", "status"),
        Index("ix_embedding_profiles_build_started", "build_started_at"),
    )

    # The set of statuses that count as "ready to activate". The
    # activate / rollback endpoints only accept profiles in this
    # set so a half-built profile can never become the production
    # index. The values are mirrored in
    # :data:`apps.api.embeddings.build_service.BUILDABLE_STATUSES`
    # / :data:`ACTIVATABLE_STATUSES` for runtime assertions.
    READY_STATUSES = ("ready", "active", "retired")

    def to_public_dict(self) -> dict:
        """Return a JSON-safe view that never leaks configuration secrets.

        The dict is what the ``/api/embeddings/status`` endpoint
        returns: enough for the operator to understand "what
        configuration did we just test?" without exposing anything
        that could be replayed against the upstream provider.
        """

        canary = self.canary_vectors
        if canary is None:
            canary_summary = None
        else:
            # We keep the canary *count* and *dimensionality* but
            # never the coordinates; the operator already saw those
            # in the test response, and they must not be persisted on
            # disk in clear text (they are a fingerprint of the
            # provider's output for our fixed probes).
            try:
                canary_summary = [
                    {"dim": len(list(vector))} for vector in canary
                ]
            except TypeError:
                canary_summary = None

        return {
            "id": self.id,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "dim": self.dim,
            "has_api_key": bool(self.has_api_key),
            "key_fingerprint": self.key_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "status": self.status,
            "canary_count": (
                len(canary) if canary is not None and not isinstance(canary, (int, float)) else 0
            ),
            "canary_dims": canary_summary,
            "last_tested_at": (
                self.last_tested_at.isoformat() if self.last_tested_at else None
            ),
            "last_error": self.last_error,
            "total_chunks": self.total_chunks,
            "completed_chunks": self.completed_chunks,
            "failed_chunks": self.failed_chunks,
            "build_started_at": (
                self.build_started_at.isoformat() if self.build_started_at else None
            ),
            "build_finished_at": (
                self.build_finished_at.isoformat() if self.build_finished_at else None
            ),
            "activated_at": (
                self.activated_at.isoformat() if self.activated_at else None
            ),
        }


class ChunkEmbedding(BaseModel):
    """A single embedding row bound to a (profile, chunk) pair.

    The vector column has a different type per dialect:

    * PostgreSQL: pgvector ``VECTOR`` without a fixed dimension, so
      profiles with different dimensions can coexist before
      profile-specific indexes are introduced.
    * SQLite: plain ``JSON`` so the test suite can keep using the
      same data path. The float values are stored as a JSON array
      and are never searched in SQLite (the test only validates the
      data contract, not the index performance).
    """

    __tablename__ = "chunk_embeddings"

    profile_id = Column(
        Integer,
        ForeignKey(
            "embedding_profiles.id",
            name="fk_chunk_embeddings_profile_id_embedding_profiles",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    chunk_id = Column(
        Integer,
        ForeignKey(
            "document_chunks.id",
            name="fk_chunk_embeddings_chunk_id_document_chunks",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    content_hash = Column(String(64), nullable=False)
    vector = Column(_VECTOR_TYPE, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "chunk_id",
            name="uix_chunk_embeddings_profile_chunk",
        ),
    )

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "profile_id": self.profile_id,
            "chunk_id": self.chunk_id,
            "content_hash": self.content_hash,
        }
