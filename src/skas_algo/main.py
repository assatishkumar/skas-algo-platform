"""CLI / server entry point."""

from __future__ import annotations

import uvicorn

from skas_algo.config import get_settings
from skas_algo.config.logging_config import configure_logging


def _export_vault(args) -> None:
    """`skas-algo export-vault` — write run-cards (and optionally the dashboards) into the vault."""
    from skas_algo.db.base import session_scope
    from skas_algo.services.vault_export import export_all, scaffold, vault_root

    if vault_root() is None:
        print("SKAS_VAULT_PATH is not set — nothing to export.")
        return
    if args.scaffold:
        print(f"scaffolded {scaffold()} dashboard notes")
    if args.backfill or not args.scaffold:
        with session_scope() as db:
            n = export_all(db)
        print(f"exported {n} run-cards to {vault_root()}")


def main() -> None:
    """CLI entry point (``skas-algo``): run the API server, or export the Obsidian vault."""
    import argparse

    parser = argparse.ArgumentParser(prog="skas-algo")
    sub = parser.add_subparsers(dest="cmd")
    ev = sub.add_parser("export-vault", help="Export run-cards into the Obsidian trading-brain vault")
    ev.add_argument("--backfill", action="store_true", help="write a run-card for every existing run")
    ev.add_argument("--scaffold", action="store_true", help="write the vault dashboards/templates")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    if args.cmd == "export-vault":
        _export_vault(args)
        return
    uvicorn.run(
        "skas_algo.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()
