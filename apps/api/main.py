"""FastAPI application entry point.

Authentication is enforced exclusively by per-router
``require_admin`` dependencies. The global middleware that
previously ran an additional DB check has been removed: it
duplicated the same query and bypassed FastAPI's
``dependency_overrides`` mechanism that the test suite relies on.
The setup / login / status endpoints are mounted as public
routers; every other business router attaches
``dependencies=[Depends(require_admin)]`` so a request without a
valid session is rejected with 401 before the handler runs.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import (
    access_tokens,
    ask,
    auth,
    categories,
    datasets,
    documents,
    embeddings,
    exports,
    files,
    health,
    mcp,
    notes,
    search,
    settings_ai,
    sources,
    v1,
    webdav,
)
from .api.auth import require_admin
from .core.config import settings

logger = logging.getLogger(__name__)


app = FastAPI(
    title="藏知 Cangzhi API",
    description="藏有所知，问有所据。个人可控的 AI 知识中枢。",
    version="0.1.0",
)

# CORS: the API is consumed by the Next.js front end in the same
# deployment. Origins are enumerated explicitly (no wildcard) and
# credentials are kept off at the CORS layer because the session
# cookie is ``SameSite=Lax`` and is sent on same-origin requests
# automatically. The middleware exists primarily so local
# development on a different port (e.g. ``localhost:3000``) can
# still issue requests.
_default_origins = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)
_extra_origins = tuple(
    origin.strip()
    for origin in (settings.cors_allowed_origins or "").split(",")
    if origin.strip()
)
_allowed_origins = _default_origins + _extra_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_allowed_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
    max_age=600,
)

# Public routers — anyone may call these.
app.include_router(health.router, prefix="/api", tags=["health"])
app.include_router(auth.router, prefix="/api", tags=["auth"])
app.include_router(v1.router, prefix="/api")
app.include_router(mcp.router, prefix="/api")

# Protected routers — every endpoint requires an authenticated
# admin. ``Depends(require_admin)`` reads the session cookie and
# returns the ``Admin`` row or 401s. Tests override the
# ``require_admin`` symbol via ``app.dependency_overrides`` to
# inject a stub admin without going through the cookie flow.
_admin_dep = [Depends(require_admin)]
app.include_router(
    settings_ai.router, prefix="/api", dependencies=_admin_dep, tags=["settings"]
)
app.include_router(access_tokens.router, prefix="/api")
app.include_router(notes.router, prefix="/api", dependencies=_admin_dep)
app.include_router(files.router, prefix="/api", dependencies=_admin_dep)
app.include_router(documents.router, prefix="/api", dependencies=_admin_dep)
app.include_router(datasets.router, prefix="/api", dependencies=_admin_dep)
app.include_router(sources.router, prefix="/api", dependencies=_admin_dep)
app.include_router(categories.router, prefix="/api", dependencies=_admin_dep)
app.include_router(categories.tags_router, prefix="/api", dependencies=_admin_dep)
app.include_router(search.router, prefix="/api", dependencies=_admin_dep)
app.include_router(ask.router, prefix="/api", dependencies=_admin_dep)
app.include_router(embeddings.router, prefix="/api", dependencies=_admin_dep)
app.include_router(webdav.router, prefix="/api", dependencies=_admin_dep)
app.include_router(exports.router, prefix="/api", dependencies=_admin_dep)


@app.get("/")
async def root():
    return {"service": "cangzhi-api", "status": "ok", "version": "0.1.0"}
