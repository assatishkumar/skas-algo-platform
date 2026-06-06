"""CLI / server entry point."""

from __future__ import annotations

import uvicorn

from skas_algo.config import get_settings
from skas_algo.config.logging_config import configure_logging


def main() -> None:
    """Run the API server (dev entry point: ``skas-algo``)."""
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run(
        "skas_algo.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()
