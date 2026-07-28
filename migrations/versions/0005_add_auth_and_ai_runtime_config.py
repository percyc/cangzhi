"""Add single-user auth (admins, sessions) and AI runtime config (M3-3)

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-28 21:00:00

This migration introduces the storage for the M3-3 milestone:

* ``admins`` and ``ai_runtime_configs`` are single-row tables. The
  application logic checks the row count first, but a unique
  constraint on ``singleton_key`` is the database-level safety net
  so a second insert fails even if a buggy client bypasses the
  pre-check.
* ``auth_sessions`` stores the server-side half of the session
  cookie. The cookie carries a 32-byte URL-safe random token; we
  only persist the SHA-256 of the token. ``expires_at`` is checked
  on every request, and ``revoked_at`` is used by ``/logout``.

The previous draft of this migration used a PostgreSQL ``ENUM``
type for the provider column; the field is now a plain ``VARCHAR``
with a ``CHECK`` constraint so SQLite (used in the test suite) and
PostgreSQL share the same schema without a custom DDL hook.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_AI_PROVIDER_VALUES = ("disabled", "openai", "ollama")


def upgrade() -> None:
    # --- admins --------------------------------------------------------
    op.create_table(
        "admins",
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
        sa.Column(
            "singleton_key",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'singleton'"),
        ),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("password_salt", sa.String(length=64), nullable=False),
        sa.Column(
            "password_algo",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'scrypt'"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uix_admins_username"),
        sa.UniqueConstraint("singleton_key", name="uix_admins_singleton"),
        sa.CheckConstraint(
            "singleton_key = 'singleton'",
            name="ck_admins_singleton_key",
        ),
    )
    op.create_index(
        op.f("ix_admins_username"),
        "admins",
        ["username"],
        unique=True,
    )

    # --- auth_sessions -------------------------------------------------
    op.create_table(
        "auth_sessions",
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
        sa.Column("admin_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["admin_id"],
            ["admins.id"],
            name="fk_auth_sessions_admin_id_admins",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uix_auth_sessions_token_hash"),
    )
    op.create_index(
        op.f("ix_auth_sessions_admin_id"),
        "auth_sessions",
        ["admin_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_auth_sessions_token_hash"),
        "auth_sessions",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "ix_auth_sessions_admin_active",
        "auth_sessions",
        ["admin_id", "revoked_at"],
        unique=False,
    )
    op.create_index(
        "ix_auth_sessions_expires_at",
        "auth_sessions",
        ["expires_at"],
        unique=False,
    )

    # --- ai_runtime_configs -------------------------------------------
    op.create_table(
        "ai_runtime_configs",
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
        sa.Column(
            "singleton_key",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'singleton'"),
        ),
        sa.Column(
            "provider",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'disabled'"),
        ),
        sa.Column("openai_base_url", sa.String(length=512), nullable=True),
        sa.Column("openai_model", sa.String(length=255), nullable=True),
        sa.Column("openai_api_key_cipher", sa.Text(), nullable=True),
        sa.Column(
            "has_api_key",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("ollama_base_url", sa.String(length=512), nullable=True),
        sa.Column("ollama_model", sa.String(length=255), nullable=True),
        sa.Column(
            "timeout_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("30"),
        ),
        sa.Column(
            "prompt_version",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'v1'"),
        ),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column(
            "extra",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["admins.id"],
            name="fk_ai_runtime_configs_updated_by_admins",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("singleton_key", name="uix_ai_runtime_configs_singleton"),
        sa.CheckConstraint(
            "singleton_key = 'singleton'",
            name="ck_ai_runtime_configs_singleton_key",
        ),
        sa.CheckConstraint(
            f"provider in {_AI_PROVIDER_VALUES}",
            name="ck_ai_runtime_configs_provider",
        ),
        sa.CheckConstraint(
            "timeout_seconds >= 1 AND timeout_seconds <= 600",
            name="ck_ai_runtime_configs_timeout",
        ),
    )


def downgrade() -> None:
    op.drop_table("ai_runtime_configs")
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_admin_active", table_name="auth_sessions")
    op.drop_index(
        op.f("ix_auth_sessions_token_hash"),
        table_name="auth_sessions",
    )
    op.drop_index(
        op.f("ix_auth_sessions_admin_id"),
        table_name="auth_sessions",
    )
    op.drop_table("auth_sessions")
    op.drop_index(op.f("ix_admins_username"), table_name="admins")
    op.drop_table("admins")
