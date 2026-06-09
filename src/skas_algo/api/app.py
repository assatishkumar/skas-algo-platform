"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from skas_algo import __version__
from skas_algo.config import get_settings

from .routes import backtest, brokers, data, health, live

logger = logging.getLogger("skas_algo")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Rebuild any paper/live runs that were still running before a restart.
    try:
        from skas_algo.live.recovery import recover_running_sessions

        recover_running_sessions()
    except Exception:  # pragma: no cover - never block startup
        logger.exception("live-session recovery failed")
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

    return app


# Module-level app for `uvicorn skas_algo.api.app:app`
app = create_app()
