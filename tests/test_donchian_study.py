"""Donchian breakout study: cycle anchoring + the per-name channel/flip state machine."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from skas_algo.services.donchian_study import (
    StudyParams,
    analyze_name_cycle,
    monthly_cycles,
    run_study,
)


def _weekdays(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


TDS = _weekdays(date(2024, 1, 1), date(2024, 4, 30))


def _bars(rows: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    """[(date, high, low, close)] -> trade-window bars frame."""
    return pd.DataFrame(
        {"date": [r[0] for r in rows], "high": [r[1] for r in rows],
         "low": [r[2] for r in rows], "close": [r[3] for r in rows]}
    )


P = StudyParams(buffer_pct=0.5, basis="touch", max_flips=3)


# ------------------------------------------------------------------ cycles

def test_monthly_cycles_expiry_anchored():
    cycles = monthly_cycles(TDS, date(2024, 1, 1), date(2024, 4, 30))
    # Anchors: last Thursdays 25 Jan / 29 Feb / 28 Mar / 25 Apr → two tradeable cycles.
    assert [c["cycle_id"] for c in cycles] == ["2024-03", "2024-04"]
    c = cycles[0]
    assert c["range_start"] == date(2024, 1, 26)   # day after the Jan expiry
    assert c["range_end"] == date(2024, 2, 29)     # the Feb expiry (previous FULL cycle)
    assert c["entry_date"] == date(2024, 3, 1)     # first trading day after the Feb expiry
    assert c["expiry"] == date(2024, 3, 28)


def test_monthly_cycles_real_expiries_override():
    # A listed weekly (Mar 7) must NOT displace the monthly; the month's LAST real expiry
    # (Mar 26, e.g. holiday-shifted off the calendar-expected 28th) becomes the anchor.
    real = [date(2024, 2, 29), date(2024, 3, 7), date(2024, 3, 26)]
    cycles = monthly_cycles(TDS, date(2024, 1, 1), date(2024, 4, 30), real_expiries=real)
    by_id = {c["cycle_id"]: c for c in cycles}
    assert by_id["2024-03"]["expiry"] == date(2024, 3, 26)
    assert by_id["2024-04"]["range_end"] == date(2024, 3, 26)


# ---------------------------------------------------------- channel tracker

def test_stays_inside():
    bars = _bars([(f"2024-03-0{i}", 1040, 960, 1000) for i in range(1, 8)])
    rec = analyze_name_cycle(bars, 1050.0, 950.0, P)
    assert rec["status"] == "inside" and rec["first_breach_side"] is None
    assert rec["flip_count"] == 0 and not rec["closed_by_flips"]


def test_buffer_edge_and_day_index():
    # 1055.24 is INSIDE the 0.5% buffer over 1050 (needs ≥ 1055.25); day 3 clears it.
    bars = _bars([
        ("2024-03-01", 1055.24, 990, 1000),
        ("2024-03-04", 1040, 990, 1000),
        ("2024-03-05", 1056.0, 990, 1030),
    ])
    rec = analyze_name_cycle(bars, 1050.0, 950.0, P)
    assert rec["first_breach_side"] == "up" and rec["first_breach_day"] == 3
    assert rec["status"] == "breakout"


def test_touch_vs_close_divergence():
    # High pierces the buffered edge but the close stays inside: touch breaches, close doesn't.
    bars = _bars([("2024-03-01", 1060, 990, 1020)])
    assert analyze_name_cycle(bars, 1050.0, 950.0, P)["first_breach_side"] == "up"
    close_p = StudyParams(buffer_pct=0.5, basis="close", max_flips=3)
    assert analyze_name_cycle(bars, 1050.0, 950.0, close_p)["first_breach_side"] is None


def test_re_entry_then_whipsaw():
    bars = _bars([
        ("2024-03-01", 1070, 1000, 1060),   # up breakout
        ("2024-03-04", 1040, 1000, 1020),   # close back strictly inside → re-entry
        ("2024-03-05", 1030, 940, 950),     # breaches the low edge again → whipsaw
    ])
    rec = analyze_name_cycle(bars, 1050.0, 950.0, P)
    assert rec["re_entered"] and rec["re_entry_day"] == 2
    assert rec["whipsaw"] and rec["whipsaw_side"] == "down"
    assert rec["status"] == "whipsaw" and rec["both_sides_breached"]


def test_gap_entry_starts_single_sided():
    # Entry close already above the range high → live sells the ATM PE only; the flip
    # simulator's first level is the entry close on the PE side.
    bars = _bars([
        ("2024-03-01", 1075, 1060, 1070),   # gapped above 1050 at entry
        ("2024-03-04", 1070, 1000, 1010),   # low 1000 ≤ 1070×0.995 → PE breach → flip 1
    ])
    rec = analyze_name_cycle(bars, 1050.0, 950.0, P)
    assert rec["breakout_at_entry"] == "up"
    assert rec["flip_count"] == 1 and rec["flips"][0]["side"] == "PE"


# ------------------------------------------------------------ flip simulator

def test_flip_rolls_then_max_flips_closes():
    bars = _bars([
        ("2024-03-01", 1040, 960, 1000),    # inside
        ("2024-03-04", 1060, 1000, 1055),   # CE breach (1060 ≥ 1055.25) → roll to PE@1055
        ("2024-03-05", 1060, 1000, 1050),   # PE level 1049.7; low 1000 → breach → roll CE@1050
        ("2024-03-06", 1058, 1000, 1056),   # CE level 1055.25; high 1058 → 3rd breach → CLOSED
        ("2024-03-07", 1200, 900, 1000),    # closed — no further flips counted
    ])
    rec = analyze_name_cycle(bars, 1050.0, 950.0, P)
    assert [f["side"] for f in rec["flips"]] == ["CE", "PE", "CE"]
    assert [f["action"] for f in rec["flips"]] == ["roll", "roll", "close"]
    assert rec["closed_by_flips"] and rec["closed_day"] == 4 and rec["flip_count"] == 3


def test_one_flip_per_bar_even_when_both_levels_hit():
    # A single wild bar clears BOTH edges: the larger excursion wins, one flip only.
    bars = _bars([("2024-03-01", 1080, 948, 1000)])  # up 2.9% beyond vs down 0.2%
    rec = analyze_name_cycle(bars, 1050.0, 950.0, P)
    assert rec["flip_count"] == 1 and rec["flips"][0]["side"] == "CE"


# ------------------------------------------------------------------ run_study

def test_run_study_aggregates_and_index_exclusion():
    cycles = monthly_cycles(TDS, date(2024, 1, 1), date(2024, 4, 30))
    n = len(TDS)
    flat = pd.DataFrame({"date": TDS, "high": [1040.0] * n, "low": [960.0] * n,
                         "close": [1000.0] * n})
    trend = pd.DataFrame({"date": TDS, "high": [900 + 3 * i for i in range(n)],
                          "low": [880 + 3 * i for i in range(n)],
                          "close": [890 + 3 * i for i in range(n)]})
    frames = {"FLAT": flat, "TREND": trend, "NIFTY 50": flat.copy()}
    res = run_study(frames, cycles, lambda d: 14.0, StudyParams())
    assert res["aggregates"]["names"] == 2            # the index is NOT a stock
    assert res["aggregates"]["cycles"] == 2
    league = {r["symbol"]: r for r in res["league"]}
    assert league["NIFTY 50"]["is_index"]
    assert league["FLAT"]["breach_rate"] == 0.0
    assert league["TREND"]["breach_rate"] == 100.0    # a steady uptrend breaks out every cycle
    assert res["vix_split"][0]["bucket"] == "<15" and res["vix_split"][0]["cycles"] == 2
    assert all(r["cycle_id"] in ("2024-03", "2024-04") for r in res["detail"])
