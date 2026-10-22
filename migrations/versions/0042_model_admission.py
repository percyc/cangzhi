"""Cross-process shared model-admission quota pools (batch 2).

Adds two tables:
  * ``model_admission_pools``  - the distributable concurrency limit, an
    optional per-pool OCR sub-limit, and optional 429 cooldown for one
    non-secret pool id.
  * ``model_admission_leases`` - an active holder (owner token) of one slot
    in a pool until ``lease_until``.

The default configuration is a SINGLE conservative pool (total 2, background 1,
OCR sub-limit 1) so chat/embedding/OCR sharing one upstream never multiply
their concurrency; operators may split the pool with the pool-map setting.
This is 0042: it chains after the monitoring per-metric state (0041) and
before the task lease/ownership migration owned by the batch-3 executor
(0043). See ADR-034.
"""
from alembic import op
import sqlalchemy as sa

revision = "0042"
down_revision = "0041"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "model_admission_pools",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("pool_id", sa.String(64), nullable=False, unique=True),
        sa.Column("capacity", sa.Integer, nullable=False),
        sa.Column("background_capacity", sa.Integer, nullable=False),
        sa.Column("ocr_sublimit", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cooldown_retry_after", sa.Integer, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_model_admission_pools_pool_id", "model_admission_pools", ["pool_id"])
    op.create_table(
        "model_admission_leases",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("pool_id", sa.String(64), nullable=False, index=True),
        sa.Column("owner_token", sa.String(64), nullable=False, unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_model_admission_leases_pool_until",
        "model_admission_leases",
        ["pool_id", "lease_until"],
    )


def downgrade():
    op.drop_index("ix_model_admission_leases_pool_until", table_name="model_admission_leases")
    op.drop_table("model_admission_leases")
    op.drop_index("ix_model_admission_pools_pool_id", table_name="model_admission_pools")
    op.drop_table("model_admission_pools")