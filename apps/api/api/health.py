from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db import get_db
from ..core.build_info import build_info
from .auth import require_admin

router = APIRouter()


@router.get("/system/version", dependencies=[Depends(require_admin)])
async def runtime_version():
    """Authenticated image metadata only; never inspect git or environment keys."""
    return build_info()


@router.get("/liveness")
async def liveness_check():
    """Liveness check - returns 200 OK as long as process is alive"""
    return {"status": "ok"}


@router.get("/readiness")
async def readiness_check(db: AsyncSession = Depends(get_db)):
    """Readiness check - verifies database connection is working"""
    try:
        await db.execute(text("SELECT 1"))
        return {
            "status": "ok",
            "database": "ok"
        }
    except Exception:
        raise HTTPException(status_code=503, detail="Database not ready") from None
