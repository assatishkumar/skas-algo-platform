"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from skas_algo import __version__
from skas_algo.config import get_settings

from .deps import require_auth
from .routes import auth, backtest, brokers, data, health, live, research, trade

logger = logging.getLogger("skas_algo")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure the schema exists (idempotent — only creates missing tables, never alters
    # existing data). Picks up new tables like greeks_snapshot on the next restart.
    try:
        import skas_algo.db.models  # noqa: F401 - register all ORM tables
        from skas_algo.db.base import Base, get_engine

        Base.metadata.create_all(get_engine())
    except Exception:  # pragma: no cover - never block startup
        logger.exception("schema create_all failed")
    # Rebuild any paper/live runs that were still running before a restart — in a
    # BACKGROUND thread. Recovery of ~20 runs does real broker/cache I/O and used to run
    # inline here, leaving the API completely unresponsive for minutes after every
    # restart/reload during market hours (2026-07-07). The API now serves immediately;
    # runs appear as they recover (start_loop/publish hop onto this loop thread-safely).
    try:
        import asyncio
        import threading

        from skas_algo.live.manager import manager
        from skas_algo.live.recovery import recover_running_sessions

        manager.broadcaster.loop = asyncio.get_running_loop()
        # Watchdog (restarts dead run loops) + daily DB backup, one singleton task.
        manager.start_maintenance()

        def _recover() -> None:
            try:
                # Snapshot the DB BEFORE recovery mutates anything — captures the last known
                # good state each restart, independent of the daily backup.
                from skas_algo.services.backup import backup_db

                backup_db()
                recover_running_sessions()
            except Exception:  # pragma: no cover - never crash the app
                logger.exception("live-session recovery failed")

        threading.Thread(target=_recover, daemon=True, name="skas-recovery").start()
    except Exception:  # pragma: no cover - never block startup
        logger.exception("live-session recovery failed to start")
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="skas-algo-platform",
        version=__version__,
        description="Backtest, forward-test, and live trading from one engine.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # OPEN routes: health (liveness) + auth (login). The WebSocket rides its own dep-free
    # router and self-gates (a browser can't send an auth header on a WS — see live.py).
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(live.ws_router, prefix="/api/v1")

    # PROTECTED routes: require a valid JWT bearer token when auth is configured (fail-open
    # otherwise — see deps.require_auth). One dependency, applied per router.
    protected = [Depends(require_auth)]
    app.include_router(backtest.router, prefix="/api/v1", dependencies=protected)
    app.include_router(brokers.router, prefix="/api/v1", dependencies=protected)
    app.include_router(data.router, prefix="/api/v1", dependencies=protected)
    app.include_router(live.router, prefix="/api/v1", dependencies=protected)
    app.include_router(research.router, prefix="/api/v1", dependencies=protected)
    app.include_router(trade.router, prefix="/api/v1", dependencies=protected)

    # Serve the built React SPA (prod single-origin) — added LAST so /api/v1/* wins. Opt-in
    # (SKAS_SERVE_WEBAPP); local dev leaves it off and keeps using the Vite dev server.
    if settings.serve_webapp:
        _mount_webapp(app)

    return app


def _mount_webapp(app: FastAPI) -> None:
    """Serve web/dist as a single-page app: real files from disk, everything else →
    index.html so client-side (React Router) deep links resolve. No-op with a clear log if
    the build is missing. The SPA is intentionally UNAUTHENTICATED at the static layer — the
    login gate is the JWT on /api/v1 (the app shell is not a secret; the data behind it is)."""
    from pathlib import Path

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    settings = get_settings()
    # Default: <repo>/web/dist — app.py is src/skas_algo/api/app.py → parents[3] is the root.
    dist = (Path(settings.webapp_dist) if settings.webapp_dist else (
        Path(__file__).resolve().parents[3] / "web" / "dist"
    )).resolve()
    index = dist / "index.html"
    if not index.is_file():
        logger.warning("SKAS_SERVE_WEBAPP is on but no SPA build at %s — run `npm run build`; "
                       "serving API only", dist)
        return

    # Built assets (hashed JS/CSS/img under /assets, plus favicon etc.) straight off disk.
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def _spa_root() -> FileResponse:
        return FileResponse(index)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_fallback(full_path: str) -> FileResponse:
        # A real top-level file (favicon.ico, manifest, robots.txt, …) → serve it; anything
        # else is a client-side route → hand back the shell. /api/* never reaches here (the
        # API routers are registered first and match first).
        candidate = (dist / full_path).resolve()
        if dist in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)

    logger.info("serving SPA from %s", dist)


# Module-level app for `uvicorn skas_algo.api.app:app`
app = create_app()
