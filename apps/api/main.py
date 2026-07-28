from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .core.config import settings
from .api import documents, files, health, notes

app = FastAPI(title="藏知 API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(notes.router, prefix="/api")
app.include_router(files.router, prefix="/api")
app.include_router(documents.router, prefix="/api")


@app.get("/")
async def root():
    return {"service": "cangzhi-api", "status": "ok", "version": "0.1.0"}
