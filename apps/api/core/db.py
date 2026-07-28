from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

# Convert to asyncpg URL
database_url = settings.database_url.replace(
    "postgresql://", "postgresql+asyncpg://"
).replace(
    "postgres://", "postgres+asyncpg://"
)

engine = create_async_engine(database_url, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
