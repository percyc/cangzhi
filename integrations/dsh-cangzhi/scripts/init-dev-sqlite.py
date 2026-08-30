"""Initialize a local SQLite database for the no-Docker DSH demo.

Production deployments should keep using Alembic with PostgreSQL/pgvector.
This helper mirrors the test-suite's supported SQLite path and is deliberately
idempotent so a developer can restart the integrated stack without data loss.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

# Running a file puts its own directory, not the repository root, on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import apps.api.models  # noqa: F401 - register every mapped table
from apps.api.core.db import AsyncSessionLocal, Base, engine
from apps.api.models.taxonomy import DEFAULT_CATEGORY_SLUGS, Category
from apps.api.models.workspaces import DEFAULT_WORKSPACE_SLUG, Workspace


async def main() -> None:
    if engine.url.get_backend_name() != "sqlite":
        raise SystemExit("init-dev-sqlite.py refuses non-SQLite DATABASE_URL values")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        workspace = (
            await session.execute(
                select(Workspace).where(Workspace.slug == DEFAULT_WORKSPACE_SLUG)
            )
        ).scalar_one_or_none()
        if workspace is None:
            workspace = Workspace(
                slug=DEFAULT_WORKSPACE_SLUG,
                name="默认空间",
                is_default=True,
                status="active",
                settings={},
            )
            session.add(workspace)
            await session.flush()

        existing = set(
            (
                await session.execute(
                    select(Category.slug).where(Category.workspace_id == workspace.id)
                )
            ).scalars()
        )
        for slug, name in DEFAULT_CATEGORY_SLUGS:
            if slug not in existing:
                session.add(Category(slug=slug, name=name, workspace_id=workspace.id))
        await session.commit()


if __name__ == "__main__":
    asyncio.run(main())
