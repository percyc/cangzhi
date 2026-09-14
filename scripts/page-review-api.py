"""Disposable SQLite API for browser page review; never connects to production."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile


def main():
    project = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project))
    with tempfile.TemporaryDirectory(prefix="cangzhi-page-review-") as directory:
        # Settings resolves .env relative to cwd. Keep the review isolated.
        os.chdir(directory)
        os.environ.update(
            DATABASE_URL=f"sqlite+aiosqlite:///{directory}/review.db",
            STORAGE_PATH=f"{directory}/storage",
            AI_PROVIDER="",
            OPENAI_API_KEY="",
            CANGZHI_SECRET_KEY="",
        )
        from apps.api.main import app
        from apps.api.core.db import Base, engine, AsyncSessionLocal
        from apps.api.models.workspaces import Workspace
        import uvicorn

        async def prepare():
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with AsyncSessionLocal() as db:
                db.add(Workspace(slug="default", name="默认空间", is_default=True,
                                 status="active", settings={}))
                await db.commit()
            await engine.dispose()

        asyncio.run(prepare())
        uvicorn.run(app, host="127.0.0.1", port=18081, access_log=False)


if __name__ == "__main__":
    main()
