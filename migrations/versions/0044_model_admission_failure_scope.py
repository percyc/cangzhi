"""Failure-isolation scope for shared model admission (ADR-034 follow-up).

Adds the nullable ``failure_scope`` column used to bind one outbound request to
one **opaque** channel scope.  The stored value is a short hex digest computed
by ``apps.api.services.model_admission.failure_scope``; the raw purpose, URL,
model name or credential are never written.

The scoped circuit-breaker state deliberately **reuses** the existing
``model_admission_pools`` / ``model_admission_leases`` tables instead of adding
a new pair of tables:

* A scope row is a ``model_admission_pools`` row whose ``pool_id`` starts with
  the reserved prefix ``scope:`` (``scope:<digest>``).  Because ``pool_id`` is
  already a unique non-secret identifier, no new storage is required and an
  operator can see/inspect it with the same tools.
* A scope "probe" is an ordinary ``model_admission_leases`` row on that scope
  pool.  ``FOR UPDATE`` on the scope row plus the unique ``owner_token`` gives
  cross-process single-probe semantics, and ``lease_until`` provides the finite
  TTL so a crashed prober cannot wedge the scope forever.
* Scoped rows still consume the **shared** quota of the real pool: acquiring a
  scope probe always happens in addition to, never instead of, the real lease.

This migration only adds a nullable column, so it is compatible with 0042/0043
and the downgrade is a plain column drop.  Nothing here clears or shortens an
already-live cooldown.
"""
from alembic import op
import sqlalchemy as sa

revision = "0044"
down_revision = "0043"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "model_admission_leases",
        sa.Column("failure_scope", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_model_admission_leases_scope",
        "model_admission_leases",
        ["pool_id", "failure_scope"],
    )


def downgrade():
    op.drop_index("ix_model_admission_leases_scope", table_name="model_admission_leases")
    op.drop_column("model_admission_leases", "failure_scope")