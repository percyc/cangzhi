"""Add external visual OCR configuration columns to ai_runtime_configs.

The OCR channel lives alongside the chat and embedding channels; it is
never a side effect of either one. All columns are additive so an
installation that never sets ``ocr_provider`` keeps the previous
behaviour with tesseract alone, and ``has_ocr_api_key`` makes the
front-end never see the ciphertext.

Revision ID: 0031
Revises: 0030
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


_OCR_PROVIDER_VALUES = ("disabled", "openai")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {column["name"] for column in inspector.get_columns("ai_runtime_configs")}

    with op.batch_alter_table("ai_runtime_configs") as batch:
        if "ocr_provider" not in existing:
            batch.add_column(
                sa.Column(
                    "ocr_provider",
                    sa.String(length=16),
                    nullable=False,
                    server_default=sa.text("'disabled'"),
                )
            )
        if "ocr_base_url" not in existing:
            batch.add_column(sa.Column("ocr_base_url", sa.String(length=512), nullable=True))
        if "ocr_model" not in existing:
            batch.add_column(sa.Column("ocr_model", sa.String(length=255), nullable=True))
        if "ocr_api_key_cipher" not in existing:
            batch.add_column(sa.Column("ocr_api_key_cipher", sa.Text(), nullable=True))
        if "has_ocr_api_key" not in existing:
            batch.add_column(
                sa.Column(
                    "has_ocr_api_key",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("false"),
                )
            )
        if "ocr_timeout_seconds" not in existing:
            batch.add_column(
                sa.Column(
                    "ocr_timeout_seconds",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("30"),
                )
            )
        if "ocr_confidence_threshold" not in existing:
            batch.add_column(
                sa.Column(
                    "ocr_confidence_threshold",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("600"),
                )
            )
        if "ocr_min_chars" not in existing:
            batch.add_column(
                sa.Column(
                    "ocr_min_chars",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("8"),
                )
            )
        if "ocr_max_external_pages" not in existing:
            batch.add_column(
                sa.Column(
                    "ocr_max_external_pages",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("20"),
                )
            )

    # The check constraints must exist exactly once. Re-applying the
    # migration on a database that already has them is a no-op; this
    # also keeps the SQLite/Postgres path symmetric.
    existing_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("ai_runtime_configs")
    }
    with op.batch_alter_table("ai_runtime_configs") as batch:
        if "ck_ai_runtime_configs_ocr_provider" not in existing_checks:
            batch.create_check_constraint(
                "ck_ai_runtime_configs_ocr_provider",
                f"ocr_provider in {_OCR_PROVIDER_VALUES}",
            )
        if "ck_ai_runtime_configs_ocr_timeout" not in existing_checks:
            batch.create_check_constraint(
                "ck_ai_runtime_configs_ocr_timeout",
                "ocr_timeout_seconds >= 1 AND ocr_timeout_seconds <= 600",
            )
        if "ck_ai_runtime_configs_ocr_confidence" not in existing_checks:
            batch.create_check_constraint(
                "ck_ai_runtime_configs_ocr_confidence",
                "ocr_confidence_threshold >= 0 AND ocr_confidence_threshold <= 1000",
            )
        if "ck_ai_runtime_configs_ocr_min_chars" not in existing_checks:
            batch.create_check_constraint(
                "ck_ai_runtime_configs_ocr_min_chars",
                "ocr_min_chars >= 0 AND ocr_min_chars <= 1000",
            )
        if "ck_ai_runtime_configs_ocr_max_external_pages" not in existing_checks:
            batch.create_check_constraint(
                "ck_ai_runtime_configs_ocr_max_external_pages",
                "ocr_max_external_pages >= 0 AND ocr_max_external_pages <= 1000",
            )


def downgrade() -> None:
    with op.batch_alter_table("ai_runtime_configs") as batch:
        batch.drop_constraint(
            "ck_ai_runtime_configs_ocr_max_external_pages", type_="check"
        )
        batch.drop_constraint("ck_ai_runtime_configs_ocr_min_chars", type_="check")
        batch.drop_constraint("ck_ai_runtime_configs_ocr_confidence", type_="check")
        batch.drop_constraint("ck_ai_runtime_configs_ocr_timeout", type_="check")
        batch.drop_constraint("ck_ai_runtime_configs_ocr_provider", type_="check")
        batch.drop_column("ocr_max_external_pages")
        batch.drop_column("ocr_min_chars")
        batch.drop_column("ocr_confidence_threshold")
        batch.drop_column("ocr_timeout_seconds")
        batch.drop_column("has_ocr_api_key")
        batch.drop_column("ocr_api_key_cipher")
        batch.drop_column("ocr_model")
        batch.drop_column("ocr_base_url")
        batch.drop_column("ocr_provider")
