"""Add independent embedding channel to AI runtime config (ADR-015 phase 1).

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-29 02:20:00

This migration introduces the first slice of ADR-015: the embedding
side gets its own provider, base URL, API key, timeout and ``has_api_key``
flag so the chat path and the embedding path can evolve independently.

* The new columns mirror the chat-side columns on purpose: same
  widths, same nullability. The ``has_embedding_api_key`` boolean is
  kept in lock-step with ``embedding_api_key_cipher`` so the API can
  answer "do we have a key?" without ever decrypting.
* The legacy ``embedding_model`` column is intentionally retained.
  Migrations are additive; the model name is still a single string
  and only the surrounding credentials move.
* A safe backfill runs against every existing row so the operator
  does not have to re-enter anything. The rule is:
    - if ``embedding_model`` is set and the chat ``provider`` is
      ``openai`` or ``ollama``, copy the matching base URL, key and
      flag over and set ``embedding_provider`` to the same value.
    - otherwise leave the row at the disabled defaults so a user
      who never enabled embedding continues to see "off".
  The ``timeout_seconds`` value is mirrored unconditionally because
  the operator clearly expected the embedding call to share the
  chat-side timeout.
* No new CHECK constraints: the existing chat-side constraints
  apply on the embedding columns through the same string widths
  enforced at the application layer. Keeping this migration
  additive avoids surprising SQLite/PostgreSQL drift in tests.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_runtime_configs",
        sa.Column(
            "embedding_provider",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'disabled'"),
        ),
    )
    op.add_column(
        "ai_runtime_configs",
        sa.Column("embedding_base_url", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "ai_runtime_configs",
        sa.Column("embedding_api_key_cipher", sa.Text(), nullable=True),
    )
    op.add_column(
        "ai_runtime_configs",
        sa.Column(
            "has_embedding_api_key",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "ai_runtime_configs",
        sa.Column(
            "embedding_timeout_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("30"),
        ),
    )

    # Safe backfill: only rows that already had an ``embedding_model``
    # and a chat ``provider`` of ``openai`` or ``ollama`` inherit the
    # matching channel. Everyone else stays at the "disabled" defaults
    # so a user who never enabled the feature does not suddenly see a
    # stale key, and a row that uses ``provider = 'disabled'`` never
    # surfaces a chat-side credential to the embedding probe.
    op.execute(
        """
        UPDATE ai_runtime_configs
           SET embedding_provider = provider
         WHERE embedding_model IS NOT NULL
           AND provider IN ('openai', 'ollama')
        """
    )
    # The timeout is mirrored unconditionally: an operator who has
    # already tuned the chat-side timeout is the same person who will
    # be sending the first embedding call, so the same budget applies.
    op.execute(
        """
        UPDATE ai_runtime_configs
           SET embedding_timeout_seconds = timeout_seconds
        """
    )
    op.execute(
        """
        UPDATE ai_runtime_configs
           SET embedding_base_url = openai_base_url
         WHERE provider = 'openai' AND embedding_model IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE ai_runtime_configs
           SET embedding_base_url = ollama_base_url
         WHERE provider = 'ollama' AND embedding_model IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE ai_runtime_configs
           SET embedding_api_key_cipher = openai_api_key_cipher,
               has_embedding_api_key = has_api_key
         WHERE provider = 'openai' AND embedding_model IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("ai_runtime_configs", "embedding_timeout_seconds")
    op.drop_column("ai_runtime_configs", "has_embedding_api_key")
    op.drop_column("ai_runtime_configs", "embedding_api_key_cipher")
    op.drop_column("ai_runtime_configs", "embedding_base_url")
    op.drop_column("ai_runtime_configs", "embedding_provider")
