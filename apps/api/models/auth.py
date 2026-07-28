from __future__ import annotations

from datetime import datetime, timezone

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

from .base import BaseModel


# --- AI runtime config ----------------------------------------------------

# Provider values are stored as plain strings and constrained at the
# application and database level. We deliberately avoid a native
# ``Enum`` so the same migration runs unchanged on PostgreSQL and
# SQLite (the test backend).
_AI_PROVIDER_VALUES = ("disabled", "openai", "ollama")

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
    timeout_seconds = Column(Integer, nullable=False, server_default=text("30"))
    prompt_version = Column(String(64), nullable=False, server_default=text("'v1'"))
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
            "timeout_seconds": self.timeout_seconds,
            "prompt_version": self.prompt_version,
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
