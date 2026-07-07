"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from skas_algo import __version__
from skas_algo.config import get_settings

from .routes import backtest, brokers, data, health, live, research, trade

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

        def _recover() -> None:
            try:
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

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(backtest.router, prefix="/api/v1")
    app.include_router(brokers.router, prefix="/api/v1")
    app.include_router(data.router, prefix="/api/v1")
    app.include_router(live.router, prefix="/api/v1")
    app.include_router(research.router, prefix="/api/v1")
    app.include_router(trade.router, prefix="/api/v1")

    return app


# Module-level app for `uvicorn skas_algo.api.app:app`
app = create_app()
