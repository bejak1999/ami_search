"""FastAPI application entrypoint.

Serves the JSON API under /api and the built single-page frontend from
/app/static, so the whole thing ships as one container with one port.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import (
    alerts,
    auth,
    channels,
    collection,
    dashboard,
    discover,
    items,
    search,
    system,
    watches,
)
from .config import settings
from .db import init_db, session_scope
from .events import bus
from .providers import close_all
from .scheduler.engine import engine
from .services import crawler, fx

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("amisearch")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("Starting %s", settings.app_name)
    init_db()
    bus.bind_loop(asyncio.get_running_loop())

    try:
        with session_scope() as db:
            fx.ensure_fresh(db)
    except Exception:  # noqa: BLE001 - never block startup on a rate provider
        log.warning("Could not load exchange rates at startup", exc_info=True)

    try:
        with session_scope() as db:
            crawler.ensure_scopes(db)
            system.load_mfc_session(db)
    except Exception:  # noqa: BLE001
        log.warning("Could not prepare the catalogue crawler", exc_info=True)

    engine.start()
    try:
        yield
    finally:
        log.info("Shutting down")
        engine.shutdown()
        close_all()


app = FastAPI(
    title="AmiSearch",
    description="Price tracking and restock alerts for Japanese hobby shops.",
    version=system.VERSION,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

app.add_middleware(GZipMiddleware, minimum_size=800)

_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


for router in (
    system.router,
    auth.router,
    search.router,
    items.router,
    watches.router,
    alerts.router,
    channels.router,
    collection.router,
    dashboard.router,
    discover.router,
    system.admin,
):
    app.include_router(router, prefix="/api")


@app.exception_handler(Exception)
async def unhandled_error(_request: Request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Something went wrong. Check the server logs for details."},
    )


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

if STATIC_DIR.is_dir():
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        """Serve real files when they exist, otherwise hand back index.html.

        The router lives in the browser, so any unknown path has to reach the
        SPA rather than 404 at the server.
        """
        candidate = (STATIC_DIR / full_path).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            candidate = STATIC_DIR / "index.html"
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")

else:  # pragma: no cover - development without a built frontend

    @app.get("/", include_in_schema=False)
    async def dev_root() -> JSONResponse:
        return JSONResponse(
            {
                "app": settings.app_name,
                "api_docs": "/api/docs",
                "note": "Frontend bundle not built. Run 'npm run build' in frontend/.",
            }
        )
