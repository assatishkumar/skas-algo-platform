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


def _import_gfd(args) -> None:
    """`skas-algo import-gfd <files/dirs…>` — load purchased GlobalDataFeeds 1-min CSVs
    into the option-intraday Parquet store (same layout the daily capture writes)."""
    from skas_algo.data.gfd_import import import_gfd

    summary = import_gfd(args.paths)
    print(
        f"imported {summary['files']} file(s) → {summary['rows']} rows "
        f"({summary['skipped_tickers']} non-option tickers skipped)"
    )
    for day, rows in sorted(summary["days"].items()):
        print(f"  {day}: {rows} rows in store")
    backup_dir = get_settings().option_bars_backup_dir
    if backup_dir:
        from skas_algo.data.option_intraday_store import mirror_store

        b = mirror_store(backup_dir)
        print(f"mirrored to backup: {b['copied']} copied, {b['skipped']} unchanged → {b['dir']}")


def _restore_option_bars(args) -> None:
    """`skas-algo restore-option-bars --from <vps-url>` — pull the 1-min option-bar days this
    box missed from a remote (VPS) store over Tailscale. Gap-fill by default; --overwrite re-pulls
    all remote days. Auth = the VPS operator password (--password / $SKAS_RESTORE_PASSWORD /
    prompt); --no-auth for an open remote."""
    import getpass
    import os

    from skas_algo.services.option_restore import login, restore_from

    token = None
    if not args.no_auth:
        pw = (
            args.password
            or os.environ.get("SKAS_RESTORE_PASSWORD")
            or getpass.getpass("VPS password: ")
        )
        if pw:
            try:
                token = login(args.base_url, pw)
            except Exception as exc:  # noqa: BLE001 — fall back to an open endpoint
                print(f"login failed ({exc}); trying without auth…")
    result = restore_from(
        args.base_url,
        token=token,
        days=args.days,
        overwrite=args.overwrite,
        progress=lambda d, i, n: print(f"  [{i}/{n}] {d}"),
    )
    print(
        f"remote days: {result['remote']} · already had: {result['already']} · "
        f"restored: {len(result['restored'])} · skipped: {len(result['skipped'])} · "
        f"errors: {len(result['errors'])}"
    )
    if result["restored"]:
        print("  restored:", ", ".join(result["restored"]))
    if result["skipped"]:
        print("  skipped (pruned off the remote):", ", ".join(result["skipped"]))
    if result["errors"]:
        print("  errors:", ", ".join(result["errors"]))


def _backfill_vix(args) -> None:
    """`skas-algo backfill-vix --since YYYY-MM-DD` — fill the minute India VIX store from
    Kite through any logged-in Zerodha account (read-only historical calls, ~60-day chunks,
    ~3 req/s). The console's strip reads it at the cursor's minute in replay."""
    from datetime import date

    from skas_algo.data.intraday_bars import backfill_vix, vix_cached_range
    from skas_algo.services.console_margin import _account

    acct = _account()
    if acct is None:
        raise SystemExit("no logged-in Zerodha account — log in on the Brokers page first")
    label, adapter = acct
    since = date.fromisoformat(args.since)
    print(f"backfilling INDIA VIX 1-min bars since {since} via {label}; store now: "
          f"{vix_cached_range()}")
    out = backfill_vix(adapter, since)
    print(f"done: {out['rows']} rows in [{since}, today]; store range {out['after']}")


def _build_iv_history(args) -> None:
    """`skas-algo build-iv-history [--underlying U]` — append every captured day the daily
    ATM IV history lacks (the console's true IV rank). ~0.35 s a day; safe to re-run."""
    from skas_algo.services.atm_iv_history import build

    unders = [args.underlying.upper()] if args.underlying else ["NIFTY", "BANKNIFTY", "SENSEX"]
    for u in unders:
        def prog(i, n, _u=u):
            if i % 100 == 0:
                print(f"  {_u}: {i}/{n}", flush=True)
        print(u, build(u, progress=prog))


def _us_daily_refresh(args) -> None:
    """`skas-algo us-daily-refresh [sp500 nasdaq100 | AAPL MSFT …]` — fetch or top up the
    US daily-bar store from Yahoo (one call per symbol, ~0.25 s apart; ten years on a
    first fetch, the tail afterwards). A universe name expands to its current list
    (refreshed from Wikipedia first). No broker, no order path."""
    import time

    from skas_algo.data import universes, us_daily, us_universe

    names, symbols = [], []
    for a in args.targets or ["sp500", "nasdaq100"]:
        (names if a.lower() in universes.US_UNIVERSES else symbols).append(a)
    for n in names:
        res = us_universe.refresh(n.lower(), baseline=list(universes.UNIVERSES[n.lower()][1]))
        status = "ok" if res.get("ok") else f"FAILED {res.get('error')}"
        print(f"universe {n}: {status} · {res.get('count')} names · "
              f"+{len(res.get('added', []))} −{len(res.get('dropped', []))}")
        for s in universes.current(n.lower()):
            if s not in symbols:
                symbols.append(s)
    t0 = time.time()
    print(f"refreshing {len(symbols)} symbols into {us_daily.store_dir()} …")

    def prog(done, total, sym, res):
        if not res.get("ok"):
            print(f"  {sym}: FAILED {res.get('error')}")
        elif done % 25 == 0 or done == total:
            print(f"  {done}/{total} · {sym} {res.get('rows')} rows → {res.get('last')}")
    out = us_daily.refresh(symbols, progress=prog)
    ok = sum(1 for r in out.values() if r.get("ok"))
    print(f"done: {ok} ok, {len(out) - ok} failed, {round(time.time() - t0)} s; "
          f"store: {us_daily.coverage()}")


def _sim_dossier(args) -> None:
    """`skas-algo sim-dossier <id> [--out file.md]` — the Simulator strategy's dossier."""
    from skas_algo.db.base import session_scope
    from skas_algo.services.simulator import dossier_markdown

    with session_scope() as db:
        text = dossier_markdown(db, int(args.sim_id))
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        print(f"wrote {args.out} ({len(text)} chars)")
    else:
        print(text)


def main() -> None:
    """CLI entry point (``skas-algo``): run the API server, or export the Obsidian vault."""
    import argparse

    parser = argparse.ArgumentParser(prog="skas-algo")
    sub = parser.add_subparsers(dest="cmd")
    ev = sub.add_parser(
        "export-vault", help="Export run-cards into the Obsidian trading-brain vault"
    )
    ev.add_argument(
        "--backfill", action="store_true", help="write a run-card for every existing run"
    )
    ev.add_argument("--scaffold", action="store_true", help="write the vault dashboards/templates")
    sub.add_parser("hash-password", help="Hash an operator password for SKAS_AUTH_PASSWORD_HASH")
    mt = sub.add_parser("mint-token", help="Mint a long-lived operator JWT (for a peer box's "
                                           "SKAS_PEER_API_TOKEN); needs SKAS_AUTH_JWT_SECRET")
    mt.add_argument("--days", type=int, default=365, help="lifetime in days (default 365)")
    ig = sub.add_parser(
        "import-gfd", help="Import GlobalDataFeeds 1-min CSVs into the option-bar store"
    )
    ig.add_argument("paths", nargs="+", help="GFD csv files and/or directories of them")
    bv = sub.add_parser(
        "backfill-vix", help="Fill the minute India VIX store from Kite (needs a Zerodha login)"
    )
    bv.add_argument("--since", default="2021-07-29",
                    help="first day to fetch (default 2021-07-29, the option store's first day)")
    ih = sub.add_parser(
        "build-iv-history", help="Append the daily ATM IV history from the 1-min option store"
    )
    ih.add_argument("--underlying", default=None, help="one underlying (default: all three)")
    ud = sub.add_parser("us-daily-refresh",
                        help="Fetch / top up US daily bars from Yahoo (universes or symbols)")
    ud.add_argument("targets", nargs="*",
                    help="sp500 / nasdaq100 and/or symbols; default: both universes")
    sd = sub.add_parser("sim-dossier", help="Write a Simulator strategy's dossier (Markdown)")
    sd.add_argument("sim_id", help="the Simulator strategy id (/simulator?id=…)")
    sd.add_argument("--out", default=None, help="file to write (default: stdout)")
    rb = sub.add_parser(
        "restore-option-bars", help="Pull missed 1-min option-bar days from a remote (VPS) store"
    )
    rb.add_argument(
        "--from",
        dest="base_url",
        required=True,
        help="remote base URL, e.g. https://<vps>.<tailnet>.ts.net",
    )
    rb.add_argument("--days", type=int, default=30, help="recent days to consider (default 30)")
    rb.add_argument(
        "--password",
        default=None,
        help="VPS operator password (else $SKAS_RESTORE_PASSWORD, else prompt)",
    )
    rb.add_argument("--no-auth", action="store_true", help="remote has auth disabled — skip login")
    rb.add_argument(
        "--overwrite",
        action="store_true",
        help="re-pull ALL remote days (default: only days this box is missing)",
    )
    ds = sub.add_parser(
        "demo-seed",
        help="Write synthetic NIFTY option days into the 1-min store (scripts/demo.sh — set "
             "SKAS_OPTION_INTRADAY_DIR first; refuses to write into the real store)",
    )
    ds.add_argument("--days", type=int, default=5, help="trading days to write (default 5)")
    ds.add_argument("--end", default=None, help="last day, YYYY-MM-DD (default: last Friday)")
    args = parser.parse_args()

    if args.cmd == "demo-seed":
        from skas_algo.services.demo_seed import seed

        for line in seed(days=args.days, end=args.end):
            print(line)
        return
    if args.cmd == "hash-password":
        _hash_password()
        return
    if args.cmd == "mint-token":
        from skas_algo.security.auth import create_token

        print(create_token(ttl_hours=int(args.days) * 24))
        return

    settings = get_settings()
    configure_logging(settings.log_level)
    if args.cmd == "export-vault":
        _export_vault(args)
        return
    if args.cmd == "import-gfd":
        _import_gfd(args)
        return
    if args.cmd == "backfill-vix":
        _backfill_vix(args)
        return
    if args.cmd == "build-iv-history":
        _build_iv_history(args)
        return
    if args.cmd == "sim-dossier":
        _sim_dossier(args)
        return
    if args.cmd == "us-daily-refresh":
        _us_daily_refresh(args)
        return
    if args.cmd == "restore-option-bars":
        _restore_option_bars(args)
        return
    uvicorn.run(
        "skas_algo.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()
