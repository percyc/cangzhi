from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
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

from .base import BaseModel

# --- AI runtime config ----------------------------------------------------

# Provider values are stored as plain strings and constrained at the
# application and database level. We deliberately avoid a native
# ``Enum`` so the same migration runs unchanged on PostgreSQL and
# SQLite (the test backend).
_AI_PROVIDER_VALUES = ("disabled", "openai", "ollama")
# External visual OCR (ADR-015 phase 2 / OCR stage 2) is an
# independent channel that lives alongside the chat and embedding
# configurations. It is never a side-effect of either one: a user
# may keep the chat model local (Ollama) while sending image
# pages to a remote vision model, or vice versa. Keeping the OCR
# values as plain strings mirrors the chat/embedding providers so
# the same migration runs unchanged on PostgreSQL and SQLite.
_OCR_PROVIDER_VALUES = ("disabled", "openai")

# A fixed singleton key so the ``admins`` and ``ai_runtime_configs``
# tables can enforce "exactly one row" through a UNIQUE index. Any
# insert with a different value is rejected by the database, which
# makes the rule work even when an attacker manages to skip the
# application-level check.
_ADMIN_SINGLETON_KEY = "singleton"
_AI_SINGLETON_KEY = "singleton"


class AIRuntimeConfig(BaseModel):
    """Single-row table holding the active AI runtime configuration.

    M3-3 introduces a web-managed configuration that overrides the
    ``.env`` AI settings. The encryption envelope for ``api_key`` is
    documented in :mod:`apps.api.security.secrets`. The provider
    column accepts ``disabled``, ``openai`` and ``ollama``; the
    page can also turn AI off without clearing every field.
    """

    __tablename__ = "ai_runtime_configs"

    singleton_key = Column(
        String(32),
        nullable=False,
        server_default=text(f"'{_AI_SINGLETON_KEY}'"),
    )
    provider = Column(
        String(16),
        nullable=False,
        server_default=text("'disabled'"),
    )
    openai_base_url = Column(String(512), nullable=True)
    openai_model = Column(String(255), nullable=True)
    openai_api_key_cipher = Column(Text, nullable=True)
    has_api_key = Column(
        Boolean, nullable=False, server_default=text("false")
    )
    ollama_base_url = Column(String(512), nullable=True)
    ollama_model = Column(String(255), nullable=True)
    embedding_model = Column(String(255), nullable=True)
    timeout_seconds = Column(Integer, nullable=False, server_default=text("30"))
    prompt_version = Column(String(64), nullable=False, server_default=text("'v1'"))
    # --- Independent embedding channel (ADR-015 phase 1) ---------------
    # The embedding side carries its own provider, base URL, API key and
    # timeout so swapping the chat provider (or rotating its key) does
    # not affect the embedding pipeline and vice versa. The fields are
    # additive: a user who never enabled embedding keeps seeing "off".
    embedding_provider = Column(
        String(16),
        nullable=False,
        server_default=text("'disabled'"),
    )
    embedding_base_url = Column(String(512), nullable=True)
    embedding_api_key_cipher = Column(Text, nullable=True)
    has_embedding_api_key = Column(
        Boolean, nullable=False, server_default=text("false")
    )
    embedding_timeout_seconds = Column(
        Integer, nullable=False, server_default=text("30")
    )
    # --- Active embedding profile pointer (ADR-015 phase 2) ---------------
    # The profile is owned by ``embedding_profiles``; this column
    # only points at the row that currently serves retrieval. The
    # column is nullable: an empty deployment has no active profile.
    # The migration that introduced this column also installs the
    # matching foreign key with ``ON DELETE SET NULL`` so a
    # accidentally-deleted profile row cannot crash reads.
    active_embedding_profile_id = Column(
        Integer,
        ForeignKey(
            "embedding_profiles.id",
            name="fk_ai_runtime_configs_active_profile_embedding_profiles",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    # --- External visual OCR channel (OCR stage 2) ------------------------
    # A separate, pluggable provider that runs *after* the local
    # tesseract pass on PDF pages that look like scans. The OCR
    # channel carries its own provider, base URL, API key and
    # timeout so it can stay open even when the chat side is
    # disabled (e.g. local-only chat + remote vision). The
    # confidence threshold and per-document external page cap
    # gate how aggressively the remote model is invoked. All
    # columns are additive: an installation that never sets
    # ``ocr_provider`` keeps the previous behaviour with tesseract
    # alone, and ``has_ocr_api_key`` makes the front-end never see
    # the ciphertext.
    ocr_provider = Column(
        String(16),
        nullable=False,
        server_default=text("'disabled'"),
    )
    ocr_base_url = Column(String(512), nullable=True)
    ocr_model = Column(String(255), nullable=True)
    ocr_api_key_cipher = Column(Text, nullable=True)
    has_ocr_api_key = Column(
        Boolean, nullable=False, server_default=text("false")
    )
    ocr_timeout_seconds = Column(
        Integer, nullable=False, server_default=text("30")
    )
    ocr_confidence_threshold = Column(
        Integer, nullable=False, server_default=text("600")
    )
    ocr_min_chars = Column(
        Integer, nullable=False, server_default=text("8")
    )
    ocr_max_external_pages = Column(
        Integer, nullable=False, server_default=text("20")
    )
    updated_by = Column(
        Integer,
        ForeignKey(
            "admins.id",
            name="fk_ai_runtime_configs_updated_by_admins",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    extra = Column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint("singleton_key", name="uix_ai_runtime_configs_singleton"),
        CheckConstraint(
            "singleton_key = 'singleton'",
            name="ck_ai_runtime_configs_singleton_key",
        ),
        CheckConstraint(
            f"provider in {_AI_PROVIDER_VALUES}",
            name="ck_ai_runtime_configs_provider",
        ),
        CheckConstraint(
            "timeout_seconds >= 1 AND timeout_seconds <= 600",
            name="ck_ai_runtime_configs_timeout",
        ),
        CheckConstraint(
            f"ocr_provider in {_OCR_PROVIDER_VALUES}",
            name="ck_ai_runtime_configs_ocr_provider",
        ),
        CheckConstraint(
            "ocr_timeout_seconds >= 1 AND ocr_timeout_seconds <= 600",
            name="ck_ai_runtime_configs_ocr_timeout",
        ),
        CheckConstraint(
            "ocr_confidence_threshold >= 0 AND ocr_confidence_threshold <= 1000",
            name="ck_ai_runtime_configs_ocr_confidence",
        ),
        CheckConstraint(
            "ocr_min_chars >= 0 AND ocr_min_chars <= 1000",
            name="ck_ai_runtime_configs_ocr_min_chars",
        ),
        CheckConstraint(
            "ocr_max_external_pages >= 0 AND ocr_max_external_pages <= 1000",
            name="ck_ai_runtime_configs_ocr_max_external_pages",
        ),
    )

    def to_public_dict(self) -> dict:
        """Return a JSON-safe view that never exposes the API key."""

        return {
            "provider": self.provider,
            "openai_base_url": self.openai_base_url,
            "openai_model": self.openai_model,
            "has_api_key": bool(self.has_api_key),
            "ollama_base_url": self.ollama_base_url,
            "ollama_model": self.ollama_model,
            "embedding_model": self.embedding_model,
            "timeout_seconds": self.timeout_seconds,
            "prompt_version": self.prompt_version,
            "embedding_provider": self.embedding_provider,
            "embedding_base_url": self.embedding_base_url,
            "has_embedding_api_key": bool(self.has_embedding_api_key),
            "embedding_timeout_seconds": self.embedding_timeout_seconds,
            "active_embedding_profile_id": self.active_embedding_profile_id,
            "ocr_provider": self.ocr_provider,
            "ocr_base_url": self.ocr_base_url,
            "ocr_model": self.ocr_model,
            "has_ocr_api_key": bool(self.has_ocr_api_key),
            "ocr_timeout_seconds": self.ocr_timeout_seconds,
            "ocr_confidence_threshold": self.ocr_confidence_threshold,
            "ocr_min_chars": self.ocr_min_chars,
            "ocr_max_external_pages": self.ocr_max_external_pages,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Admin(BaseModel):
    """Single-user admin record.

    The login identity is always the lowercase username. The password
    is stored as a scrypt hash plus the parameters used to derive it;
    see :mod:`apps.api.security.passwords`. The ``singleton_key``
    column enforces a single-row table at the database level so the
    application-level check is just a UX nicety.
    """

    __tablename__ = "admins"

    singleton_key = Column(
        String(32),
        nullable=False,
        server_default=text(f"'{_ADMIN_SINGLETON_KEY}'"),
    )
    username = Column(String(64), nullable=False, unique=True, index=True)
    password_hash = Column(String(512), nullable=False)
    password_salt = Column(String(64), nullable=False)
    password_algo = Column(String(16), nullable=False, server_default=text("'scrypt'"))
    is_active = Column(Boolean, nullable=False, server_default=text("true"))
    last_login_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "singleton_key", name="uix_admins_singleton"
        ),
        CheckConstraint(
            "singleton_key = 'singleton'",
            name="ck_admins_singleton_key",
        ),
    )


class AuthSession(BaseModel):
    """Server-side session record.

    The cookie carries a 32-byte URL-safe random token; we only
    persist the SHA-256 of the token so a database leak does not
    yield reusable credentials. ``expires_at`` is the absolute
    deadline checked on every request.
    """

    __tablename__ = "auth_sessions"

    admin_id = Column(
        Integer,
        ForeignKey(
            "admins.id",
            name="fk_auth_sessions_admin_id_admins",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    user_agent = Column(String(512), nullable=True)
    ip_address = Column(String(64), nullable=True)
    last_seen_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_auth_sessions_admin_active", "admin_id", "revoked_at"),
        Index("ix_auth_sessions_expires_at", "expires_at"),
    )

    @property
    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        expires = self.expires_at
        if expires is None:
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > datetime.now(tz=timezone.utc)


class PersonalAccessToken(BaseModel):
    """Personal Access Token for API authentication.

    Tokens are always owned by an admin (single-user system today).
    Only the SHA-256 hash of the token is stored; the plaintext is
    only returned once at creation time. A short prefix is kept for
    identification without exposing the full hash.
    """

    __tablename__ = "personal_access_tokens"

    admin_id = Column(
        Integer,
        ForeignKey(
            "admins.id",
            name="fk_personal_access_tokens_admin_id_admins",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    name = Column(String(255), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    token_prefix = Column(String(15), nullable=False, index=True)
    scopes = Column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=False,
        server_default=text("'[]'"),
    )
    expires_at = Column(DateTime(timezone=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    # When set the token is bound to a single workspace and the holder cannot
    # switch to a different workspace via headers / query / cookies. The
    # column is nullable for legacy personal-use tokens that select a workspace
    # through the request.
    workspace_id = Column(
        Integer,
        ForeignKey(
            "workspaces.id",
            name="fk_personal_access_tokens_workspace_id_workspaces",
            ondelete="CASCADE",
        ),
        nullable=True,
        index=True,
    )

    __table_args__ = (
        Index("ix_personal_access_tokens_admin_active", "admin_id", "revoked_at"),
        Index("ix_personal_access_tokens_expires_at", "expires_at"),
        Index("ix_personal_access_tokens_workspace_active", "workspace_id", "revoked_at"),
    )

    def to_public_dict(self) -> dict:
        """Return a safe public representation without sensitive fields."""
        return {
            "id": self.id,
            "admin_id": self.admin_id,
            "name": self.name,
            "token_prefix": self.token_prefix,
            "scopes": self.scopes,
            "workspace_id": self.workspace_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @property
    def is_valid(self) -> bool:
        """Check if the token is currently usable."""
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None:
            expires = self.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= datetime.now(tz=timezone.utc):
                return False
        return True

    def has_scope(self, scope: str) -> bool:
        """Check if this token has the given scope."""
        return scope in (self.scopes or [])
