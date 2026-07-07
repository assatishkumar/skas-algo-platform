"""CLI / server entry point."""

from __future__ import annotations

import uvicorn

from skas_algo.config import get_settings
from skas_algo.config.logging_config import configure_logging


def _hash_password() -> None:
    """Prompt for an operator password (twice) and print its bcrypt hash to paste into
    SKAS_AUTH_PASSWORD_HASH. The password is never stored or echoed."""
    import getpass

    from skas_algo.security import hash_password

    pw = getpass.getpass("New operator password: ")
    if not pw:
        raise SystemExit("empty password — aborted")
    if pw != getpass.getpass("Confirm password: "):
        raise SystemExit("passwords did not match — aborted")
    print("\nAdd this to your .env (keep it secret):\n")
    print(f"SKAS_AUTH_PASSWORD_HASH={hash_password(pw)}")


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
    sub.add_parser("hash-password", help="Hash an operator password for SKAS_AUTH_PASSWORD_HASH")
    args = parser.parse_args()

    if args.cmd == "hash-password":
        _hash_password()
        return

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
