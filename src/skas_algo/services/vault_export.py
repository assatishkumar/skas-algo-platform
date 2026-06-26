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


def scaffold() -> int:
    """Write the vault dashboards/templates (Dataview leaderboards + consistency). Returns files written."""
    root = vault_root()
    if root is None:
        return 0
    notes = {
        "Dashboards/Leaderboard.md": (
            {"type": "dashboard"},
            "# Leaderboard\n\n```dataview\n"
            "table mode, return_pct, max_dd_pct, win_rate, trades, regime, outcome\n"
            'from "Runs" sort return_pct desc\n```\n',
        ),
        "Dashboards/Consistency.md": (
            {"type": "dashboard"},
            "# Backtest → forward → live consistency\n\n"
            "Compare each strategy across modes — does the backtest hold up live?\n\n"
            "```dataview\n"
            "table rows.mode as modes, rows.return_pct as returns, rows.win_rate as win_rates\n"
            'from "Runs" group by strategy\n```\n',
        ),
        "Dashboards/Regime.md": (
            {"type": "dashboard"},
            "# Performance by regime\n\n```dataview\n"
            "table rows.strategy as strategies, rows.return_pct as returns\n"
            'from "Runs" where regime group by regime\n```\n',
        ),
    }
    n = 0
    for relpath, (fm, body) in notes.items():
        _write_note(relpath, fm, body)
        n += 1
    return n
