"""Durable WebDAV scan progress and staged file metadata."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0039"
down_revision = "0038"
branch_labels = depends_on = None


def timestamps():
    return [sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now())]


def upgrade():
    json_type = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table("webdav_scans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("webdav_sources.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("root", sa.Text(), nullable=False),
        sa.Column("pending", json_type, nullable=False),
        sa.Column("visited", json_type, nullable=False),
        sa.Column("scanned", sa.Integer(), nullable=False),
        sa.Column("files", sa.Integer(), nullable=False),
        sa.Column("entries", sa.Integer(), nullable=False),
        sa.Column("resumed", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("result", json_type), *timestamps())
    op.create_index("ix_webdav_scans_id", "webdav_scans", ["id"])
    op.create_table("webdav_scan_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scan_id", sa.Integer(), sa.ForeignKey("webdav_scans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.String(2048), nullable=False),
        sa.Column("data", json_type, nullable=False),
        sa.UniqueConstraint("scan_id", "path", name="uix_webdav_scan_file"), *timestamps())
    op.create_index("ix_webdav_scan_files_id", "webdav_scan_files", ["id"])
    op.create_index("ix_webdav_scan_files_scan_id", "webdav_scan_files", ["scan_id"])


def downgrade():
    op.drop_table("webdav_scan_files")
    op.drop_table("webdav_scans")
