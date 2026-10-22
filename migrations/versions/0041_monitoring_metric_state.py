"""Per-metric monitoring state with backward read-only snapshot mapping.

Keeps the original ``monitoring_snapshots.data`` field and read-only maps any
pre-migration group snapshots into per-metric rows so the page can distinguish
never attempted / first failed / partial / stale without rewriting facts. The
legacy group ``error`` (already a sanitized category) is carried onto the
backfilled metrics so a previously-failed old group surfaces as ``degraded``
(old values retained) instead of looking successful. See ADR-033.
"""
from alembic import op
import sqlalchemy as sa

revision = "0041"
down_revision = "0040"
branch_labels = depends_on = None

LEGACY_ERROR = "旧快照映射：最后一次采集失败，当前显示旧值。"

_metrics_table = sa.table(
    "monitoring_metrics",
    sa.column("kind", sa.String(24)),
    sa.column("metric", sa.String(48)),
    sa.column("value", sa.JSON()),
    sa.column("last_success_at", sa.DateTime(timezone=True)),
    sa.column("last_attempt_at", sa.DateTime(timezone=True)),
    sa.column("error", sa.String(160)),
    sa.column("consecutive_failures", sa.Integer()),
    sa.column("next_attempt_at", sa.DateTime(timezone=True)),
)


def upgrade():
    op.create_table(
        "monitoring_metrics",
        sa.Column("kind", sa.String(24), primary_key=True),
        sa.Column("metric", sa.String(48), primary_key=True),
        sa.Column("value", sa.JSON()),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.String(160)),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
    )
    _backfill_from_old_snapshots()


def _backfill_from_old_snapshots():
    """Map pre-migration group ``data`` into metric rows (read-only old state).

    A group row carried ``data``/``sampled_at`` on a previous success, so a
    backfilled metric represents an old successful sample: last attempt mirrors
    the snapshot's ``attempted_at``, consecutive failures start at zero, and a
    prior sanitized group ``error`` is preserved as a legacy category. Fields the
    old schema never recorded stay NULL rather than being invented.
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT kind, data, sampled_at, attempted_at, error FROM monitoring_snapshots "
            "WHERE data IS NOT NULL AND sampled_at IS NOT NULL"
        ).columns(data=sa.JSON(), sampled_at=sa.DateTime(timezone=True),
                  attempted_at=sa.DateTime(timezone=True))
    ).mappings()
    prepared = []
    for row in rows:
        data = row["data"]
        if not isinstance(data, dict):
            continue
        legacy_error = LEGACY_ERROR if row["error"] else None
        for metric, value in data.items():
            prepared.append(
                {
                    "kind": row["kind"],
                    "metric": metric,
                    "value": value,
                    "last_success_at": row["sampled_at"],
                    "last_attempt_at": row["attempted_at"],
                    "error": legacy_error,
                    "consecutive_failures": 0,
                    "next_attempt_at": None,
                }
            )
    if prepared:
        bind.execute(_metrics_table.insert().values(prepared))


def downgrade():
    op.drop_table("monitoring_metrics")
