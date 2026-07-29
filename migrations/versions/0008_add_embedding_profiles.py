"""Add embedding_profiles, chunk_embeddings and active profile pointer.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-29 12:00:00

This migration implements the data plane for ADR-015 (M3-6b phase 1):

* ``embedding_profiles`` records every "version" of the embedding
  pipeline we have ever run. A profile carries its own provider, base
  URL, model name, dimensionality, the SHA-256 of its canary vectors
  and the status from the state machine described in ADR-015
  (``draft`` / ``tested`` / ``building`` / ``ready`` / ``active`` /
  ``retired`` / ``failed``).

  A ``config_fingerprint`` column is stored alongside the canary
  fingerprint so the API can answer "is this the same configuration
  we have already validated?" without any decryption. The fingerprint
  only encodes the model + dimension + base URL + an *existence flag*
  for the API key; the plaintext key never enters the fingerprint
  computation, and rotating the key while keeping every other
  parameter unchanged must NOT change the fingerprint (and therefore
  must NOT trigger a rebuild).

* ``chunk_embeddings`` is the versioned index. Every row is a
  ``(profile_id, chunk_id, content_hash)`` triple. PostgreSQL stores
  the embedding in a ``VECTOR`` column with a fixed dimensionality
  per profile (declared via the ``dim`` column on
  ``embedding_profiles``); SQLite stores the same payload as a JSON
  array so the test suite can keep using the same data path. No
  HNSW index is created in this migration; M3-6c will add the index
  when the corpus is large enough.

* ``ai_runtime_configs.active_embedding_profile_id`` points at the
  profile that currently serves retrieval. The column is nullable:
  no profile is active until the operator has validated and built
  one. A non-null value here is the only signal the rest of the
  application needs to know which version of the index to query.

The migration is additive. It does not touch the existing
``embedding_*`` columns on ``ai_runtime_configs``; those continue to
hold the *operator's candidate* configuration, while the profile
table records every *historical* configuration. The test endpoint
this milestone introduces bridges the two by writing a new profile
row (or updating an existing one) whenever the operator validates a
new candidate.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None


_PROFILE_STATUS_VALUES = (
    "draft",
    "tested",
    "building",
    "ready",
    "active",
    "retired",
    "failed",
)


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # --- embedding_profiles ------------------------------------------------
    op.create_table(
        "embedding_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        # Provider and endpoint. The base URL is the public-facing
        # address (the same value the operator typed into the settings
        # page). It is part of the fingerprint, so it must be
        # persisted for the lifetime of the profile.
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=False),
        # Model name. Always non-null: a profile without a model is
        # not useful. Switching the model produces a new profile.
        sa.Column("model", sa.String(length=255), nullable=False),
        # Dimensionality observed during the most recent successful
        # test. The test endpoint writes this column before persisting
        # the profile, so any subsequent read can trust it. The
        # dimension participates in the fingerprint so two profiles
        # that claim to be the same but disagree on dimensionality
        # are correctly treated as different.
        sa.Column("dim", sa.Integer(), nullable=False),
        # Whether a key is needed / present. The actual key is *not*
        # stored on the profile; it remains in ``ai_runtime_configs``
        # encrypted. We only record an existence flag here.
        sa.Column("has_api_key", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # Key fingerprint derived from the encrypted key (NOT the
        # plaintext). Two configurations with the same provider +
        # base URL + model + dimension + key fingerprint are
        # considered the same vector space, even if the operator
        # rotates the underlying key.
        sa.Column("key_fingerprint", sa.String(length=128), nullable=True),
        # Deterministic fingerprint over the configuration above.
        # Used as the join key with the legacy ``ai_runtime_configs``
        # row so we can answer "is this profile still the one I just
        # tested?" without a join.
        sa.Column("config_fingerprint", sa.String(length=128), nullable=False, index=True),
        # Canary: a JSON-serialised list of canary vectors, one per
        # fixed probe text. The list length is fixed at three so
        # downstream code can assert on it. Each vector is a list of
        # floats of length ``dim``.
        sa.Column(
            "canary_vectors",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=True,
        ),
        # State-machine status. See ADR-015 §"状态机".
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        # ``last_tested_at`` is updated whenever a probe succeeds.
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        # ``last_error`` carries a *sanitised* error message from the
        # last failed test. It must not contain any key material.
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "dim > 0",
            name="ck_embedding_profiles_dim_positive",
        ),
        sa.CheckConstraint(
            f"status in {_PROFILE_STATUS_VALUES}",
            name="ck_embedding_profiles_status",
        ),
        sa.CheckConstraint(
            "provider in ('openai', 'ollama')",
            name="ck_embedding_profiles_provider",
        ),
    )
    op.create_index(
        "ix_embedding_profiles_status",
        "embedding_profiles",
        ["status"],
        unique=False,
    )

    # --- chunk_embeddings ---------------------------------------------------
    # The vector column is declared with a different type per
    # dialect:
    #
    # * PostgreSQL uses pgvector's ``VECTOR`` type without a fixed
    #   dimensionality, so the same table can host profiles that
    #   produce different-size vectors. ADR-015 explicitly defers
    #   HNSW to a later milestone — this column is left unindexed
    #   here on purpose. Per-profile HNSW indexes will be added in
    #   M3-6c once the corpus grows large enough to benefit.
    # * SQLite (used in tests) stores the same payload as a JSON
    #   array so the data path can be exercised without pgvector.
    if is_postgres:
        from pgvector.sqlalchemy import Vector

        vector_type = Vector()
    else:
        vector_type = sa.JSON()

    op.create_table(
        "chunk_embeddings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("chunk_id", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("vector", vector_type, nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["embedding_profiles.id"],
            name="fk_chunk_embeddings_profile_id_embedding_profiles",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["document_chunks.id"],
            name="fk_chunk_embeddings_chunk_id_document_chunks",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id",
            "chunk_id",
            name="uix_chunk_embeddings_profile_chunk",
        ),
    )
    op.create_index(
        "ix_chunk_embeddings_profile_id",
        "chunk_embeddings",
        ["profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_chunk_embeddings_chunk_id",
        "chunk_embeddings",
        ["chunk_id"],
        unique=False,
    )

    # --- ai_runtime_configs.active_embedding_profile_id ---------------------
    op.add_column(
        "ai_runtime_configs",
        sa.Column("active_embedding_profile_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ai_runtime_configs_active_profile_embedding_profiles",
        "ai_runtime_configs",
        "embedding_profiles",
        ["active_embedding_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_ai_runtime_configs_active_profile",
        "ai_runtime_configs",
        ["active_embedding_profile_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_runtime_configs_active_profile",
        table_name="ai_runtime_configs",
    )
    op.drop_constraint(
        "fk_ai_runtime_configs_active_profile_embedding_profiles",
        "ai_runtime_configs",
        type_="foreignkey",
    )
    op.drop_column("ai_runtime_configs", "active_embedding_profile_id")

    op.drop_index("ix_chunk_embeddings_chunk_id", table_name="chunk_embeddings")
    op.drop_index("ix_chunk_embeddings_profile_id", table_name="chunk_embeddings")
    op.drop_table("chunk_embeddings")

    op.drop_index(
        "ix_embedding_profiles_status", table_name="embedding_profiles"
    )
    op.drop_table("embedding_profiles")
