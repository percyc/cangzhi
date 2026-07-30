"""Enable legacy Word files for WebDAV sources using the original defaults.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-30 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

_OLD_DEFAULT = [".pdf", ".docx", ".md", ".markdown", ".txt"]
_NEW_DEFAULT = [".pdf", ".doc", ".docx", ".md", ".markdown", ".txt"]


def _replace_extensions(current: list[str], expected: list[str], updated: list[str]):
    return updated if current == expected else current


def upgrade() -> None:
    sources = sa.table(
        "webdav_sources",
        sa.column("id", sa.Integer()),
        sa.column("include_extensions", sa.JSON()),
    )
    connection = op.get_bind()
    for source_id, extensions in connection.execute(
        sa.select(sources.c.id, sources.c.include_extensions)
    ):
        replacement = _replace_extensions(extensions, _OLD_DEFAULT, _NEW_DEFAULT)
        if replacement != extensions:
            connection.execute(
                sources.update()
                .where(sources.c.id == source_id)
                .values(include_extensions=replacement)
            )


def downgrade() -> None:
    sources = sa.table(
        "webdav_sources",
        sa.column("id", sa.Integer()),
        sa.column("include_extensions", sa.JSON()),
    )
    connection = op.get_bind()
    for source_id, extensions in connection.execute(
        sa.select(sources.c.id, sources.c.include_extensions)
    ):
        replacement = _replace_extensions(extensions, _NEW_DEFAULT, _OLD_DEFAULT)
        if replacement != extensions:
            connection.execute(
                sources.update()
                .where(sources.c.id == source_id)
                .values(include_extensions=replacement)
            )
