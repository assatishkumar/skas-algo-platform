"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from skas_algo import __version__
from skas_algo.config import get_settings

from .routes import backtest, health


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="skas-algo-platform",
        version=__version__,
        description="Backtest, forward-test, and live trading from one engine.",
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

    return app


# Module-level app for `uvicorn skas_algo.api.app:app`
app = create_app()
