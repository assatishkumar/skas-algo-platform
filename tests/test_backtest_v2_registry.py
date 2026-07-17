"""The Backtest-v2 form registry must stay honest about the strategies it drives.

`web/src/lib/backtestV2/registry.ts` binds form controls to REAL strategy kwargs. Two ways
that rots, both silent in the browser:
  * a param is renamed/removed in Python → the form keeps sending a dead kwarg (swallowed
    by **_ignored — the control just does nothing);
  * a default drifts → a "blank" backtest stops matching the strategy's own behaviour.

And the unit trap (CLAUDE.md): profit_target_pct is a FRACTION (0.025) in the ratio family
but a WHOLE percent (2.5) in delta_neutral/iron_fly/cpre/the straddles. The form shows
whole percents and tags the fraction fields; a wrong tag turns 2.5% into 250%.

Parsing TS from Python is unusual, but the registry IS the contract between the two
languages and there is no web test runner here — this is the cheapest place to pin it.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from skas_algo.strategies.registry import get_strategy

REGISTRY_TS = Path(__file__).resolve().parents[1] / "web" / "src" / "lib" / "backtestV2" / "registry.ts"

# Params the form legitimately sends that are NOT strategy kwargs.
_HARNESS = {"margin_per_lot", "sizing", "sizing_buffer_pct", "lots", "capital_utilization_pct",
            "margin_per_lotset"}                       # replay-harness / ratio-family sizing
_SERVICE_ONLY = {"momentum_theta_gainer_intra": {"vol_multiplier", "slippage_bps", "r", "capital"}}

# Defaults that intentionally differ from the constructor (they match the OLD form's
# defaults — the "blank v2 run == blank v1 run" baseline):
#   force_entry: a monthly strategy on a short backtest window has no expiry to anchor on,
#     so the FORM has always defaulted it True (the ctor keeps False for live recovery, §1).
#   sets: the ctor takes None and resolves `sets or 1` — the form states the 1 explicitly.
_INTENTIONAL = {("delta_neutral_monthly", "force_entry"), ("iron_fly_monthly", "force_entry"),
                ("call_put_ratio_expiry", "sets")}


def _ctor_defaults(strategy_id: str) -> dict:
    """Every kwarg default across the MRO (subclass params hide behind *args/**kwargs)."""
    cls = get_strategy(strategy_id)
    out: dict = {}
    for klass in reversed(cls.__mro__):
        init = klass.__dict__.get("__init__")
        if init is None:
            continue
        for name, p in inspect.signature(init).parameters.items():
            if name in ("self", "universe", "initial_capital", "args", "kwargs", "_ignored"):
                continue
            if p.default is not inspect.Parameter.empty:
                out[name] = p.default
    return out


def _specs() -> dict[str, str]:
    """strategy_id → its slice of the registry source."""
    src = REGISTRY_TS.read_text()
    ids = re.findall(r'\n    id: "([^"]+)",', src)
    starts = {sid: src.index(f'id: "{sid}"') for sid in ids}
    end_all = src.index("export const isV2Strategy")
    out = {}
    for sid in ids:
        later = [s for s in starts.values() if s > starts[sid]]
        out[sid] = src[starts[sid]: min(later) if later else end_all]
    return out


def _fields(body: str):
    """(param, default_literal, is_fraction) for f(...) and TIME(...) entries."""
    for m in re.finditer(r'f\("([a-z0-9_]+)",\s*"[^"]*",\s*"\w+",\s*([^,)]+)(,\s*\{([^}]*)\})?',
                         body):
        param, dflt, _, opts = m.groups()
        yield param, dflt.strip(), 'unit: "fraction"' in (opts or "")
    for m in re.finditer(r'TIME\("([a-z0-9_]+)",\s*"[^"]*",\s*"([^"]+)"', body):
        yield m.group(1), f'"{m.group(2)}"', False


def _lit(s: str):
    s = s.strip()
    if s in ("true", "false"):
        return s == "true"
    if s.startswith('"'):
        return s.strip('"')
    try:
        return float(s)
    except ValueError:
        return s


def test_registry_ids_are_real_strategies():
    for sid in _specs():
        get_strategy(sid)   # raises KeyError if the id ever drifts


def test_every_registry_param_is_a_real_kwarg():
    problems = []
    for sid, body in _specs().items():
        allowed = set(_ctor_defaults(sid)) | _HARNESS | _SERVICE_ONLY.get(sid, set())
        for param, _dflt, _frac in _fields(body):
            if param not in allowed:
                problems.append(f"{sid}: '{param}' is not a constructor kwarg")
    assert not problems, "\n".join(problems)


def test_registry_defaults_match_the_strategies():
    """A blank v2 run must be the strategy's own configuration — including the ×100 the
    fraction-tagged fields undo at build time."""
    problems = []
    for sid, body in _specs().items():
        real = _ctor_defaults(sid)
        for param, dflt, is_fraction in _fields(body):
            if param not in real or (sid, param) in _INTENTIONAL:
                continue
            want, got = real[param], _lit(dflt)
            if is_fraction:
                # form shows whole percents; the strategy wants the fraction
                assert isinstance(got, float)
                if abs(float(want) * 100 - got) > 1e-9:
                    problems.append(f"{sid}.{param}: form {got}% but ctor {want} (=×100 {float(want)*100})")
            elif isinstance(want, bool) or isinstance(got, bool):
                if bool(want) != bool(got):
                    problems.append(f"{sid}.{param}: form {got} vs ctor {want}")
            elif isinstance(want, (int, float)) and isinstance(got, float):
                if abs(float(want) - got) > 1e-9:
                    problems.append(f"{sid}.{param}: form {got} vs ctor {want}")
            elif str(want) != str(got):
                problems.append(f"{sid}.{param}: form '{got}' vs ctor '{want}'")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("sid,param", sorted(
    {("batman_ratio_monthly", "profit_target_pct"), ("batman_ratio_monthly", "stop_loss_pct"),
     ("hni_weekly", "profit_target_pct"), ("hni_weekly", "stop_loss_pct")}))
def test_ratio_family_percents_are_fraction_tagged(sid, param):
    """The ratio family's %s are FRACTIONS — if these lose their tag, a 2.5% target silently
    becomes 250% of capital."""
    body = _specs()[sid]
    tagged = {p for p, _d, frac in _fields(body) if frac}
    assert param in tagged, f"{sid}.{param} must carry unit: \"fraction\""


def test_whole_percent_strategies_are_not_fraction_tagged():
    """The mirror: delta_neutral/iron_fly/cpre/straddles take WHOLE percents — a stray
    fraction tag would divide their target by 100."""
    for sid in ("delta_neutral_monthly", "iron_fly_monthly", "call_put_ratio_expiry",
                "intraday_straddle", "weekly_intraday_straddle"):
        tagged = {p for p, _d, frac in _fields(_specs()[sid]) if frac}
        assert not tagged, f"{sid} takes whole percents but tags {tagged} as fractions"
