"""Admin-only enhancement management. Business rules live in the shared service."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from sqlalchemy.ext.asyncio import AsyncSession
from ..core.db import get_db
from ..services import knowledge_enhancement as service
from ..services.enhancement_read import read_overview
from ..services.knowledge_read import KnowledgeReadError

router = APIRouter(tags=["knowledge-enhancement"])


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool = False
    modules: list[str] = Field(default_factory=list, max_length=3)
    call_budget: StrictInt = Field(default=8, ge=1, le=32)
    cost_acknowledged: StrictBool = False


class StartInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cost_acknowledged: StrictBool = False


class ResumeInput(StartInput):
    additional_calls: StrictInt = Field(ge=1, le=32)


async def invoke(db, fn, *args, write=False, **kwargs):
    try:
        result = await db.run_sync(lambda session: fn(session, *args, **kwargs))
        if write:
            await db.commit()
        return result
    except service.EnhancementError as exc:
        await db.rollback()
        raise HTTPException(status_code=exc.status, detail=str(exc)) from None


@router.get("/enhancement/settings")
async def settings(db: AsyncSession = Depends(get_db)):
    return await invoke(db, service.get_settings)


@router.put("/enhancement/settings")
async def update_settings(data: SettingsInput, db: AsyncSession = Depends(get_db)):
    return await invoke(db, service.save_settings, data.model_dump(), write=True)


@router.get("/documents/{document_id}/enhancements")
async def list_runs(document_id: int, db: AsyncSession = Depends(get_db)):
    return await invoke(db, service.list_runs, document_id)


@router.post("/documents/{document_id}/enhancements")
async def start_run(document_id: int, data: StartInput, db: AsyncSession = Depends(get_db)):
    return await invoke(db, service.start_run, document_id, **data.model_dump(), write=True)


@router.get("/enhancements/{run_id}")
async def read_run(run_id: int, offset: int = Query(0, ge=0), limit: int = Query(5, ge=1, le=10),
                   db: AsyncSession = Depends(get_db)):
    return await invoke(db, service.read_run, run_id, offset=offset, limit=limit)


@router.post("/enhancements/{run_id}/cancel")
async def cancel_run(run_id: int, db: AsyncSession = Depends(get_db)):
    return await invoke(db, service.cancel_run, run_id, write=True)


@router.get("/enhancements/{run_id}/overview")
async def overview(run_id: int, node_key: str | None = Query(None, max_length=40),
                   db: AsyncSession = Depends(get_db)):
    try:
        return await read_overview(db, run_id, node_key=node_key)
    except KnowledgeReadError as exc:
        status = 404 if exc.code.endswith("_not_found") else 409 if exc.code == "enhancement_stale" else 400
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)}) from None


@router.post("/enhancements/{run_id}/resume")
async def resume_run(run_id: int, data: ResumeInput, db: AsyncSession = Depends(get_db)):
    return await invoke(db, service.resume_run, run_id, **data.model_dump(), write=True)
