"""Export SKAS Algo activity as Markdown into an Obsidian vault — the "trading brain" memory.

Each run becomes a note with YAML frontmatter (the schema Dataview + Claude rely on) + a short
machine-written synopsis. Claude Desktop (via the Obsidian MCP) reads/searches these notes and writes
Insight notes back; Dataview renders leaderboards/consistency from the frontmatter.

No-op when ``settings.vault_path`` is unset. Idempotent: a re-export rewrites only the machine-managed
region (between ``<!-- skas:begin -->`` / ``<!-- skas:end -->``) and preserves everything below it — your
own notes and any Insight notes Claude wrote.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from skas_algo.config.settings import get_settings
from skas_algo.data.options_provider import INDEX_SYMBOL, VIX_SYMBOL

logger = logging.getLogger("skas_algo.vault")

BEGIN = "<!-- skas:begin -->"
END = "<!-- skas:end -->"
_USER_TAIL = "\n## Your notes\n\n_(add your own notes here — preserved across re-exports)_\n"


def vault_root() -> Path | None:
    vp = get_settings().vault_path
    return Path(vp).expanduser() if vp else None


# ───────────────────────────────────────────────────────── markdown helpers
def _yaml_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    if s == "" or s.strip() != s or re.search(r'[:#\[\]{}",\n]', s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _frontmatter(fm: dict) -> str:
    lines = []
    for k, v in fm.items():
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(_yaml_scalar(x) for x in v)}]")
        else:
            lines.append(f"{k}: {_yaml_scalar(v)}")
    return "\n".join(lines)


def _write_note(relpath: str, frontmatter: dict, body: str) -> Path | None:
    """Write/update a note. Machine region (frontmatter + body between markers) is replaced; whatever
    followed the END marker (your notes / Claude's insights) is preserved verbatim."""
    root = vault_root()
    if root is None:
        return None
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    machine = f"---\n{_frontmatter(frontmatter)}\n---\n{BEGIN}\n{body.rstrip()}\n{END}\n"
    tail = _USER_TAIL
    if path.exists():
        old = path.read_text(encoding="utf-8")
        i = old.find(END)
        if i != -1:
            tail = old[i + len(END):]  # keep user-authored content below the machine region
    path.write_text(machine + tail, encoding="utf-8")
    return path


# ───────────────────────────────────────────────────────── regime tagging
def _regime(sd, start: str | None, end: str | None) -> str | None:
    """A coarse market-regime label over [start, end]: '<vix bucket>-<nifty trend>' (e.g. calm-range)."""
    if sd is None or not start or not end:
        return None
    try:
        s, e = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
        nf = sd.get_prices(symbol=INDEX_SYMBOL["NIFTY"], start_date=s, end_date=e, asset_type="stock")
        vx = sd.get_prices(symbol=VIX_SYMBOL, start_date=s, end_date=e, asset_type="stock")
    except Exception:  # pragma: no cover - cache miss
        return None
    trend = "range"
    if nf is not None and len(nf) >= 2:
        c = nf["close"].astype(float)
        chg = (c.iloc[-1] - c.iloc[0]) / c.iloc[0] if c.iloc[0] else 0.0
        trend = "up" if chg > 0.03 else "down" if chg < -0.03 else "range"
    bucket = "unknown"
    if vx is not None and len(vx):
        v = float(vx["close"].astype(float).mean())
        bucket = "calm" if v < 13 else "normal" if v < 18 else "stressed"
    return f"{bucket}-{trend}"


# ───────────────────────────────────────────────────────── run cards
def _mode(run) -> str:
    return str(getattr(run.mode, "value", run.mode)).lower()


def _num(m: dict, key: str):
    v = m.get(key)
    return round(v, 2) if isinstance(v, (int, float)) else None


def _outcome(run, ret) -> str:
    if run.stopped_at is None:
        return "open"
    if ret is None:
        return "settled"
    return "win" if ret > 0 else "loss" if ret < 0 else "settled"


def build_run_card(run, algo, sd=None) -> tuple[str, dict, str]:
    """(relpath, frontmatter, body) for a run-card from an AlgoRun + its Algo."""
    m = run.metrics or {}
    h = m.get("metrics") if isinstance(m.get("metrics"), dict) else m  # headline scalars (report nests them)
    p = run.params_snapshot or {}
    mode = _mode(run)
    start = (p.get("start_date") or (run.started_at.date().isoformat() if run.started_at else None))
    end = (p.get("end_date") or (run.stopped_at.date().isoformat() if run.stopped_at else None))
    ret, cagr = _num(h, "Total Return %"), _num(h, "CAGR %")
    maxdd, win, trades = _num(h, "Max Drawdown %"), _num(h, "Win Rate %"), h.get("Total Trades")
    regime = _regime(sd, start, end)
    ic = str(getattr(algo.instrument_class, "value", algo.instrument_class)).lower()
    tags = ["options" if ic == "deriv" else "equity", algo.strategy_id]

    fm = {
        "type": "run", "run_id": run.id, "strategy": algo.strategy_id, "mode": mode,
        "window_start": start, "window_end": end, "capital": algo.capital,
        "return_pct": ret, "cagr_pct": cagr, "max_dd_pct": maxdd, "win_rate": win,
        "trades": int(trades) if isinstance(trades, (int, float)) else None,
        "regime": regime, "outcome": _outcome(run, ret), "tags": tags,
    }
    name = algo.name or f"{algo.strategy_id} #{run.id}"
    synopsis = (f"{algo.strategy_id} {mode}: {ret:+.1f}% return"
                + (f" over {start}→{end}" if start and end else "")
                + (f", max DD {maxdd:.1f}%" if maxdd is not None else "")
                + (f", win {win:.0f}%" if win is not None else "")
                + (f" across {fm['trades']} trades" if fm["trades"] else "")
                + (f" ({regime} regime)" if regime else "") + ".") if ret is not None else \
               f"{algo.strategy_id} {mode} — metrics pending."
    body = (
        f"# {name} — {mode}\n"
        f"[[{algo.strategy_id}]] · {mode}" + (f" · {start}→{end}" if start and end else "") + "\n\n"
        f"> {synopsis}\n\n"
        f"| metric | value |\n|---|---|\n"
        f"| Return | {('%+.1f%%' % ret) if ret is not None else '—'} |\n"
        f"| CAGR | {('%.1f%%' % cagr) if cagr is not None else '—'} |\n"
        f"| Max drawdown | {('%.1f%%' % maxdd) if maxdd is not None else '—'} |\n"
        f"| Win rate | {('%.0f%%' % win) if win is not None else '—'} |\n"
        f"| Trades | {fm['trades'] if fm['trades'] is not None else '—'} |\n"
        f"| Capital | ₹{algo.capital:,.0f} |\n"
        f"| Regime | {regime or '—'} |\n"
    )
    relpath = f"Runs/{start or 'NA'} {algo.strategy_id} #{run.id}.md"
    return relpath, fm, body


def build_strategy_card(strategy_id: str) -> tuple[str, dict, str]:
    """A stub strategy note whose Dataview lists every run of that strategy (backlink target)."""
    fm = {"type": "strategy", "strategy": strategy_id, "tags": [strategy_id]}
    body = (
        f"# {strategy_id}\n\n"
        "Runs of this strategy (newest first):\n\n"
        "```dataview\n"
        "table mode, window_start, return_pct, max_dd_pct, win_rate, outcome\n"
        f'from "Runs" where strategy = "{strategy_id}" sort window_start desc\n'
        "```\n"
    )
    return f"Strategies/{strategy_id}.md", fm, body


# ───────────────────────────────────────────────────────── public API
def _safe_cache():
    try:
        from skas_algo.data.provider import get_data_cache
        return get_data_cache()
    except Exception:  # pragma: no cover - cache unavailable
        return None


def export_run(run, algo, sd=None) -> Path | None:
    """Write/refresh a run-card (and ensure its strategy note exists). No-op without a vault."""
    if vault_root() is None:
        return None
    sd = sd if sd is not None else _safe_cache()
    relpath, fm, body = build_run_card(run, algo, sd)
    path = _write_note(relpath, fm, body)
    srel, sfm, sbody = build_strategy_card(algo.strategy_id)
    _write_note(srel, sfm, sbody)
    return path


def export_run_safe(run, algo, sd=None) -> None:
    """Fire-and-forget wrapper for lifecycle hooks — never breaks the caller on a vault error."""
    try:
        export_run(run, algo, sd)
    except Exception:  # pragma: no cover - vault export must never break a save/finalize
        logger.exception("vault export failed for run %s", getattr(run, "id", "?"))


def journal(kind: str, title: str, *, strategy: str | None = None, run_id: int | None = None,
            detail: str | None = None) -> Path | None:
    """Append an activity event to the daily journal note (``Journal/<date>.md``) — the "why" behind
    the runs: deploys, interventions, screens, lifecycle. Append-only; no-op without a vault."""
    root = vault_root()
    if root is None:
        return None
    from datetime import datetime

    now = datetime.now()
    path = root / "Journal" / f"{now.date().isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"---\ntype: journal\ndate: {now.date().isoformat()}\n---\n"
                        f"# Journal {now.date().isoformat()}\n\n", encoding="utf-8")
    rid = f" (#{run_id})" if run_id else ""
    link = f" [[{strategy}]]" if strategy else ""
    extra = f" — {detail}" if detail else ""
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"- **{now:%H:%M}** `{kind}` {title}{rid}{link}{extra}\n")
    return path


def journal_safe(kind: str, title: str, **kw) -> None:
    """Fire-and-forget journal write for route/manager hooks — never breaks the caller."""
    try:
        journal(kind, title, **kw)
    except Exception:  # pragma: no cover - journalling must never break an action
        logger.exception("vault journal failed: %s", title)


def export_all(session, *, sd=None) -> int:
    """Backfill: write a run-card for every existing run. Returns the count written."""
    if vault_root() is None:
        return 0
    from skas_algo.db.models import Algo, AlgoRun
    sd = sd if sd is not None else _safe_cache()
    n = 0
    for run, algo in session.query(AlgoRun, Algo).join(Algo, AlgoRun.algo_id == Algo.id).all():
        relpath, fm, body = build_run_card(run, algo, sd)
        _write_note(relpath, fm, body)
        srel, sfm, sbody = build_strategy_card(algo.strategy_id)
        _write_note(srel, sfm, sbody)
        n += 1
    return n


_RECIPES = """# Claude Desktop recipes

Point Claude Desktop's Obsidian (or filesystem) MCP at this vault, then paste a recipe.

## Weekly digest → Insights note
> Read the run-cards in `Runs/` and the events in `Journal/`. Write `Insights/<today> weekly digest.md`:
> what happened this week, what's working, anomalies (drift / churn / breaches), and 2-3 things to try.
> Cite run-cards by name. Keep it under 300 words.

## Backtest → forward → live consistency
> For each strategy with both a backtest and a paper/live run, compare return / win-rate / max-DD.
> Flag overfit (live far below backtest), slippage, and behavioural churn (flip counts). Be specific.

## Failure post-mortem
> Take run #<id> (or a named run-card). Reconstruct what happened from its card + the matching `Journal/`
> entries, explain why it under/over-performed, and write `Insights/<name> post-mortem.md`.

## Recommendations
> Across all run-cards, propose param tweaks or new variants worth backtesting, grounded in the numbers.
> For each: the change, the evidence (cite cards), and the expected effect.

## Ad-hoc Q&A
> "Which SST variant held up best live?"  ·  "Best neutral-regime options strategy?"
> "Why did the donchian basket underperform its backtest?"
"""


def scaffold() -> int:
    """Write the vault home, Dataview dashboards, journal index and Claude-Desktop recipes.
    Returns the number of notes written. Idempotent (preserves anything below the machine region)."""
    root = vault_root()
    if root is None:
        return 0
    notes = {
        "Trading Brain.md": (
            {"type": "home"},
            "# Trading Brain\n\nA memory of every SKAS Algo run + decision, for Claude Desktop to reason over.\n\n"
            "**Dashboards:** [[Leaderboard]] · [[Consistency]] · [[Regime]] · [[Journal Index]]\n\n"
            "**Use it:** [[Recipes]] — paste a recipe into Claude Desktop (Obsidian MCP) over this vault.\n\n"
            "Notes are exported by `skas-algo export-vault`; your edits below the marker are preserved.\n",
        ),
        "Dashboards/Leaderboard.md": (
            {"type": "dashboard"},
            "# Leaderboard\n\n```dataview\n"
            "table mode, return_pct as return, max_dd_pct as maxDD, win_rate as win, trades, regime, outcome\n"
            'from "Runs" sort return_pct desc\n```\n',
        ),
        "Dashboards/Consistency.md": (
            {"type": "dashboard"},
            "# Backtest → forward → live consistency\n\n"
            "Each strategy's return by mode, side by side — does the backtest hold up live?\n\n"
            "```dataview\n"
            "table\n"
            '  filter(rows, (r) => r.mode = "backtest").return_pct as backtest,\n'
            '  filter(rows, (r) => r.mode = "paper").return_pct as paper,\n'
            '  filter(rows, (r) => r.mode = "live").return_pct as live\n'
            'from "Runs"\ngroup by strategy\n```\n',
        ),
        "Dashboards/Regime.md": (
            {"type": "dashboard"},
            "# Performance by regime\n\n```dataview\n"
            "table rows.strategy as strategies, rows.return_pct as returns\n"
            'from "Runs" where regime group by regime\n```\n',
        ),
        "Dashboards/Journal Index.md": (
            {"type": "dashboard"},
            "# Journal\n\nDeploys, interventions, screens and lifecycle events (newest first).\n\n"
            "```dataview\nlist\nfrom \"Journal\" sort file.name desc\n```\n",
        ),
        "Recipes.md": ({"type": "recipes"}, _RECIPES),
    }
    n = 0
    for relpath, (fm, body) in notes.items():
        _write_note(relpath, fm, body)
        n += 1
    return n
