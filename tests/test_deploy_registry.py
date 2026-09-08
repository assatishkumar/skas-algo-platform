"""The Trade → Deploy page is REGISTRY-DRIVEN (web/src/lib/deploy/registry.ts, 2026-08-27):
every deployable strategy is declared as data and one page renders them all.

This is the test that makes that safe. Strategy ctors end in ``**_ignored``, so a form field
whose ``param`` is not a real kwarg is accepted and SILENTLY does nothing — the mechanism
behind #279 (a run asked for equal_value sizing and traded price-weighted) and the
value_investing deploy bug (the generic form showed capital_parts/profit_target while the
strategy used its own defaults). Declaring fields per strategy only helps if something
checks the declarations, so:

  * every registry id is a real registered strategy;
  * every declared ``route`` exists on the FastAPI app;
  * every field ``param`` is a real ctor kwarg of that strategy (or a field on its deploy
    model, for deploy-layer knobs like margin_per_set that the route maps through);
  * every id has a /docs card, so the summary panel is never blank.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from skas_algo.strategies.registry import available, get_strategy

_ROOT = Path(__file__).resolve().parents[1]
_REGISTRY = _ROOT / "web" / "src" / "lib" / "deploy" / "registry.ts"
_DOCS = _ROOT / "web" / "src" / "lib" / "strategyDocs.ts"

# Deploy-layer knobs the ROUTE maps onto something else, or RUN infra the generic
# /live/start path owns (not strategy kwargs at all).
_ROUTE_LEVEL = {"underlying", "underlyings", "capital", "mode", "quote_source",
                "broker_account_id", "refresh_seconds", "ignore_market_hours", "auto",
                "name", "notes", "symbols", "universe", "lookback", "tax_rate",
                "withdrawal_rate", "decision_time"}


def _ctor_kwargs(strategy_id: str) -> set[str]:
    """Every kwarg across the MRO — subclass params hide behind *args/**kwargs, so a plain
    signature() on e.g. IronFlyMonthlyStrategy shows only its OWN additions (the
    test_backtest_v2_registry._ctor_defaults approach)."""
    cls = get_strategy(strategy_id)
    out: set[str] = set()
    for klass in reversed(cls.__mro__):
        init = klass.__dict__.get("__init__")
        if init is None:
            continue
        out |= set(inspect.signature(init).parameters)
    return out


def _entries() -> list[dict]:
    """Parse the registry into {id, route, params[]} — one dict per DeploySpec."""
    src = _REGISTRY.read_text(encoding="utf-8")
    body = src[src.index("export const DEPLOY_REGISTRY"):src.index("export const GROUP_LABEL")]
    out: list[dict] = []
    # split on the top-level spec boundaries (4-space `id:` opens each)
    chunks = re.split(r"\n  \{\n    id: ", body)[1:]
    for chunk in chunks:
        sid = re.match(r'"([\w\d_]+)"', chunk).group(1)
        route = re.search(r'route: "([^"]+)"', chunk)
        params = re.findall(r'\bf\(\s*"([\w\d_]+)"', chunk)
        params += re.findall(r'\bTIME\(\s*"([\w\d_]+)"', chunk)
        if "MARGIN_PER_SET(" in chunk:
            params.append("margin_per_set")
        if "FORCE," in chunk:
            params.append("force_entry")
        # `...cadence(a, b)` adds both; `...cadence(a, b).slice(1)` drops profit_check
        # (intraday_strangle_combo has no profit decision — VWAP/leg exits only).
        for call in re.findall(r"cadence\([^)]*\)(\.slice\(1\))?", chunk):
            params += ["stop_check"] if call else ["profit_check", "stop_check"]
        fixed = re.search(r"fixed: \{([^}]*)\}", chunk)
        if fixed:
            params += re.findall(r"(\w+):", fixed.group(1))
        out.append({"id": sid, "route": route.group(1) if route else None,
                    "params": sorted(set(params)), "custom": "custom:" in chunk})
    return out


ENTRIES = _entries()


def test_the_registry_parsed():
    assert len(ENTRIES) >= 15, f"only parsed {len(ENTRIES)} — the regex drifted from the file"


@pytest.mark.parametrize("e", ENTRIES, ids=lambda e: e["id"])
def test_every_registry_id_is_a_real_strategy(e):
    assert e["id"] in available(), f"{e['id']} is not in strategies/registry.py"


@pytest.mark.parametrize("e", [e for e in ENTRIES if e["route"]], ids=lambda e: e["id"])
def test_every_declared_route_exists(e):
    from skas_algo.api.app import create_app

    paths = {r.path for r in create_app().routes if hasattr(r, "path")}
    assert "/api/v1" + e["route"] in paths, f"{e['id']} points at a route that does not exist"


@pytest.mark.parametrize("e", ENTRIES, ids=lambda e: e["id"])
def test_every_field_param_is_a_real_ctor_kwarg(e):
    """The one that matters — **_ignored makes a wrong param invisible at runtime."""
    ctor = _ctor_kwargs(e["id"])
    unknown = [p for p in e["params"] if p not in ctor and p not in _ROUTE_LEVEL]
    assert not unknown, (
        f"{e['id']}: the deploy form would send {unknown}, which the ctor does not define — "
        f"**_ignored would swallow them silently"
    )


@pytest.mark.parametrize("e", [e for e in ENTRIES if not e["custom"]], ids=lambda e: e["id"])
def test_every_deployable_strategy_has_a_docs_card(e):
    """The summary panel reads lib/strategyDocs — a missing card renders an empty panel.
    `custom` entries (custom_equity) render a bespoke form and are in the docs test's own
    _NOT_A_CARD set, so they are exempt here too."""
    docs = _DOCS.read_text(encoding="utf-8")
    assert f'    id: "{e["id"]}"' in docs, f"{e['id']} has no /docs card for the summary panel"


# Strategies that deploy from somewhere OTHER than the Trade → Deploy page, each for a
# stated reason. Anything not here and not in the registry has NO deploy path at all —
# which is exactly how intraday_strangle_combo sat live-capable but unreachable for two
# weeks (2026-08-13 → 08-27), and how the owner ended up asking where it was.
_DEPLOYS_ELSEWHERE = {
    "broker_smoke_test": "Brokers page card (a one-shot order-path probe, not a strategy)",
    "custom_options": "Trade → Build a position (a manual multi-leg position)",
    "donchian_strangle_monthly": "Trade → Screener (the basket comes from a scan)",
    "donchian_strangle_bt": "backtest-only sibling of donchian_strangle_monthly — no live path",
}


def test_every_strategy_has_a_deploy_path():
    """The registry must cover every strategy that isn't deliberately elsewhere."""
    ids = {e["id"] for e in ENTRIES}
    orphans = sorted(set(available()) - ids - set(_DEPLOYS_ELSEWHERE))
    assert not orphans, (
        f"{orphans} can be registered and run but have NO way to deploy them — add a registry "
        f"entry, or list them in _DEPLOYS_ELSEWHERE with the page that owns them"
    )


def test_the_elsewhere_list_stays_honest():
    """A strategy listed as 'deploys elsewhere' must still exist, and must NOT also be in the
    registry (two deploy paths for one strategy is what this whole page removed)."""
    ids = {e["id"] for e in ENTRIES}
    for sid in _DEPLOYS_ELSEWHERE:
        assert sid in available(), f"{sid} is listed as deployable elsewhere but is not registered"
        assert sid not in ids, f"{sid} is in BOTH the registry and _DEPLOYS_ELSEWHERE"


# ---------------------------------------------------- percent vs fraction (2026-09-08)

def _generic_number_fields() -> list[tuple[str, str, float, bool]]:
    """(strategy_id, param, form default, pct flag) for every numeric field of a spec that
    deploys through the GENERIC /live/start path (a `route` maps its own units)."""
    src = _REGISTRY.read_text(encoding="utf-8")
    body = src[src.index("export const DEPLOY_REGISTRY"):src.index("export const GROUP_LABEL")]
    out = []
    for chunk in re.split(r"\n  \{\n    id: ", body)[1:]:
        sid = re.match(r'"([\w\d_]+)"', chunk).group(1)
        if re.search(r'route: "', chunk) or "custom:" in chunk:
            continue
        for m in re.finditer(
                r'\bf\(\s*"([\w\d_]+)",\s*"([^"]*)",\s*"number",\s*([-\d.]+)(?:,\s*\{([^}]*)\})?',
                chunk):
            param, label, dflt, rest = m.groups()
            # only knobs that READ as percents — a delta or a ratio is neither
            if "%" not in label and not param.endswith("_pct"):
                continue
            out.append((sid, param, float(dflt), bool(rest and "pct: true" in rest)))
    return out


def _ctor_default(strategy_id: str, param: str):
    cls = get_strategy(strategy_id)
    for klass in reversed(cls.__mro__):
        init = klass.__dict__.get("__init__")
        if init is None:
            continue
        p = inspect.signature(init).parameters.get(param)
        if p is not None and p.default is not inspect.Parameter.empty:
            return p.default
    return None


@pytest.mark.parametrize("sid,param,form_default,pct", _generic_number_fields(),
                         ids=lambda x: str(x))
def test_a_fraction_taking_knob_is_flagged_pct_and_a_percent_taking_one_is_not(
        sid, param, form_default, pct):
    """The generic path sends form values VERBATIM to the ctor. A ctor whose default is a
    fraction (0.06) takes fractions, so its field must carry `pct: true` (the page divides
    by 100); a ctor whose default is a whole percent (2.5, 40) must not. Before this pin,
    "Profit target % = 6" reached supertrend/sst/hni/the ratio family as 6.0 — a 600%
    target that could never fire — on every deploy from the page since 2026-08-27."""
    cd = _ctor_default(sid, param)
    if not isinstance(cd, (int, float)) or isinstance(cd, bool):
        pytest.skip("no numeric ctor default to compare against")
    if float(cd) == 0:
        # 0 = "off" says nothing about units (pullback_pct is a fraction, max_spread_pct a
        # percent) — those are settled by reading the strategy, not by this pin
        pytest.skip("ctor default 0 is unit-agnostic")
    fraction_ctor = 0 < abs(float(cd)) < 1
    if fraction_ctor:
        assert pct, (f"{sid}.{param}: ctor default {cd} is a FRACTION but the field is not "
                     f"pct: true — the form would send {form_default} as {form_default}x")
    else:
        assert not pct, (f"{sid}.{param}: ctor default {cd} is not a fraction; pct: true "
                         f"would send {form_default / 100}")
