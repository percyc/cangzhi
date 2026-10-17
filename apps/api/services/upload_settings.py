"""Global upload policy shared by browser and public API writes."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..models.system_config import SystemConfig


async def get_upload_limit_mb(db: AsyncSession) -> int:
    value = await db.scalar(select(SystemConfig.max_upload_size_mb).where(SystemConfig.id == 1))
    return value if value is not None else settings.max_upload_size_mb


async def save_upload_limit_mb(db: AsyncSession, value: int) -> None:
    # Atomic singleton upsert, including concurrent first saves.
    if db.bind.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy.dialects.postgresql import insert
    statement = insert(SystemConfig).values(id=1, max_upload_size_mb=value)
    await db.execute(statement.on_conflict_do_update(
        index_elements=[SystemConfig.id], set_={"max_upload_size_mb": value},
    ))
    await db.commit()
