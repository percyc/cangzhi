from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings


def _resolve_async_url(url: str) -> str:
    """Return an async-driver URL for the configured database.

    The application ships with asyncpg for PostgreSQL and aiosqlite
    for the test suite. Anything that does not already have a
    ``+<driver>`` token is rewritten to a sensible default.
    """

    if url.startswith("sqlite://") and not url.startswith("sqlite+"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


database_url = _resolve_async_url(settings.database_url)
engine = create_async_engine(database_url, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
