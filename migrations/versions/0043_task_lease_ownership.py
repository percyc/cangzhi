"""Ordinary processing job ownership, finite lease and bounded recovery.

Adds the ADR-035 ownership fields to ``processing_jobs`` so a supervised
executor can hold a unique attempt with a bounded lifetime and a stale
attempt can never commit after the lease is recovered by a successor.

Ownership semantics (see ADR-035 for the exact limits):

* New columns are strictly additive and default to NULL/0, so pre-migration
  rows keep working and the schema downgrade is a plain column drop.
* ``attempt_id`` is a unique token stamped at claim time;
  ``lease_until`` / ``hard_deadline`` bound the executor lifetime;
  ``heartbeat_at`` is refreshed by the holder while live;
  ``worker_id`` identifies the executor process;
  ``attempt_count`` is the bounded recovery counter (kept separate from the
  application ``retry_count`` so a crash-recovery attempt is not silently
  charged to a model/parse retry budget).
* A ``processing`` row whose ``lease_until`` is NULL is a *historical orphan*
  created before this migration. It is intentionally never taken over: there
  is no fencing token proving the old executor stopped.

This migration depends on 0042 (model admission). It is part of the shared
processing schema and is coordinated with the parent session; it is not
applied against a live instance outside an isolated validation container.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0043"
down_revision = "0042"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "processing_jobs",
        sa.Column("worker_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("attempt_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "processing_jobs",
        sa.Column("hard_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "processing_jobs",
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    # Recovery scans hand-off a processing job whose lease has expired. Index
    # (status, lease_until) so that scan stays cheap and does not touch the
    # legacy ordering index used by the scheduler.
    op.create_index(
        "ix_processing_jobs_lease_recovery",
        "processing_jobs",
        ["status", "lease_until"],
        postgresql_where=sa.text("status = 'processing' AND lease_until IS NOT NULL"),
    )
    op.create_index("ix_processing_jobs_attempt_id", "processing_jobs", ["attempt_id"])


def downgrade():
    op.drop_index("ix_processing_jobs_attempt_id", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_lease_recovery", table_name="processing_jobs")
    op.drop_column("processing_jobs", "attempt_count")
    op.drop_column("processing_jobs", "hard_deadline")
    op.drop_column("processing_jobs", "heartbeat_at")
    op.drop_column("processing_jobs", "lease_until")
    op.drop_column("processing_jobs", "attempt_id")
    op.drop_column("processing_jobs", "worker_id")