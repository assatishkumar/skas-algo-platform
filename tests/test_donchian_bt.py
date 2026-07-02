"""donchian_strangle_bt end-to-end: schedule building (ranges → strikes, breakout-ATM,
premium floor, hedge sizing off the real chain) and a 2-cycle engine run exercising the
inherited live manage path — touch flips via day H/L, once/day, max_flips close-out,
expiry settlement, clean re-entry into the next cycle."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from skas_algo.data.basket_options import build_basket_options_run
from skas_algo.engine.runner import BacktestRunner
from skas_algo.services.donchian_bt import build_cycle_schedule
from skas_algo.strategies.donchian_strangle_bt import DonchianStrangleBtStrategy


def _weekdays(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


CAL = _weekdays(date(2024, 1, 2), date(2024, 4, 30))
START, END = CAL[0], CAL[-1]

# Scripted RELIANCE trade-window bars (Mar cycle): breach → roll → breach → roll →
# breach → closed; later wild bars must NOT trade (the name is closed for the cycle).
_REL_SCRIPT: dict[date, tuple[float, float, float]] = {
    date(2024, 3, 1): (1010, 990, 1000),    # entry day — inside
    date(2024, 3, 4): (1020, 990, 1010),
    date(2024, 3, 5): (1075, 1040, 1070),   # hi ≥ 1050×1.005 → flip 1 → sell PE @ ATM 1050
    date(2024, 3, 6): (1080, 1060, 1075),   # PE 1050 needs lo ≤ 1044.75 — not yet
    date(2024, 3, 7): (1070, 1000, 1005),   # lo 1000 → flip 2 → sell CE @ ATM 1000
    date(2024, 3, 8): (1002, 990, 998),     # CE 1000 needs hi ≥ 1005 — not yet
    date(2024, 3, 11): (1015, 1000, 1010),  # hi 1015 → 3rd breach → name CLOSED
    date(2024, 3, 12): (1100, 900, 1000),   # wild — but closed, must not trade
    date(2024, 3, 13): (1120, 880, 1000),
}


def _rel_bar(d: date, i: int) -> tuple[float, float, float]:
    if d in _REL_SCRIPT:
        return _REL_SCRIPT[d]
    # Range-window pattern: highs top out at 1050, lows bottom at 950 → strikes 1050/950.
    pat = [(1050, 970, 1010), (1030, 950, 990), (1040, 960, 1000)]
    return pat[i % 3]


def _infy_bar(_d: date, i: int) -> tuple[float, float, float]:
    # Oscillates 1450–1550 in the range window, stays comfortably inside afterwards.
    pat = [(1550, 1490, 1520), (1500, 1450, 1470), (1530, 1480, 1500)]
    return pat[i % 3]


class FakeSD:
    def __init__(self):
        self.frames: dict[str, pd.DataFrame] = {}
        for name, fn in (("RELIANCE", _rel_bar), ("INFY", _infy_bar)):
            rows = [fn(d, i) for i, d in enumerate(CAL)]
            self.frames[name] = pd.DataFrame({
                "date": CAL, "high": [r[0] for r in rows],
                "low": [r[1] for r in rows], "close": [r[2] for r in rows],
            })
        self.frames["NIFTY 50"] = pd.DataFrame({
            "date": CAL, "high": [22050.0] * len(CAL), "low": [21950.0] * len(CAL),
            "close": [22000.0] * len(CAL),
        })

    def get_prices(self, symbol, start_date=None, end_date=None, asset_type="stock"):
        df = self.frames.get(symbol)
        if df is None:
            return None
        if start_date is not None:
            df = df[df["date"] >= start_date]
        if end_date is not None:
            df = df[df["date"] <= end_date]
        return df.reset_index(drop=True)

    def list_option_expiries(self, underlying):
        return []  # fall back to the calendar-expected monthly anchors

    def get_option_chain(self, underlying, on_date):
        if underlying != "NIFTY":
            return None
        expiry = date(2024, 3, 28) if on_date < date(2024, 3, 29) else date(2024, 4, 25)
        rows = []
        for k in range(20000, 24001, 500):
            for right in ("CE", "PE"):
                rows.append({"trade_date": on_date, "symbol": "NIFTY", "expiry_date": expiry,
                             "strike_price": float(k), "option_type": right,
                             "close": 100.0, "settle_price": 100.0, "open_interest": 1})
        return pd.DataFrame(rows)

    def get_option_series(self, underlying, expiry, strike, right, start_date=None, end_date=None):
        return pd.DataFrame({"trade_date": CAL, "close": [100.0] * len(CAL)})


def _schedule(sd):
    return build_cycle_schedule(sd, ["RELIANCE", "INFY"], START, END,
                                skip_leg_min_premium_pct=0.0)  # tiny synthetic premiums — no floor


def test_schedule_ranges_strikes_and_hedge():
    cycles = _schedule(FakeSD())
    assert [c["expiry"] for c in cycles] == ["2024-03-28", "2024-04-25"]
    c1 = cycles[0]
    assert c1["entry_date"] == "2024-03-01"
    by = {(leg["underlying"], leg["right"]): leg for leg in c1["legs"]}
    assert by[("RELIANCE", "CE")]["strike"] == 1050.0   # prev-cycle high
    assert by[("RELIANCE", "PE")]["strike"] == 950.0    # prev-cycle low
    assert by[("RELIANCE", "CE")]["lot_size"] == 500
    assert by[("INFY", "CE")]["strike"] == 1550.0 and by[("INFY", "PE")]["strike"] == 1450.0
    # Hedge: notional 1000×500 + 1500×400 = ₹1.1M ≈ 1 NIFTY lot (22000×50); ±4.5% round-out.
    assert by[("NIFTY", "CE")]["side"] == "buy" and by[("NIFTY", "CE")]["strike"] == 23000.0
    assert by[("NIFTY", "PE")]["strike"] == 21000.0
    assert by[("NIFTY", "CE")]["lots"] == 1 and by[("NIFTY", "CE")]["lot_size"] == 50


def test_schedule_premium_floor_skips_legs():
    # An absurd floor (5% of spot) excludes every OTM stock leg → only hedge-less cycles.
    cycles = build_cycle_schedule(FakeSD(), ["RELIANCE", "INFY"], START, END,
                                  skip_leg_min_premium_pct=5.0)
    assert all(not c["legs"] for c in cycles)


def _run():
    sd = FakeSD()
    strategy = DonchianStrangleBtStrategy(
        universe=["NIFTY"], initial_capital=10_000_000,
        breach_basis="touch", breach_buffer_pct=0.5, max_flips=3,
        portfolio_sl_pct=50.0,  # keep the portfolio stop out of the flip scenario's way
    )
    strategy.set_cycles(_schedule(sd))
    mv, _chain, settler, margin = build_basket_options_run(sd, ["RELIANCE", "INFY"], START, END)
    runner = BacktestRunner(
        strategy=strategy, universe=["NIFTY"], loader=lambda *a: None,
        initial_capital=10_000_000, tax_rate=0.0,
        market_view=mv, settler=settler, margin_model=margin,
    )
    return runner.run(START, END), strategy


def test_end_to_end_flips_settlement_and_reentry():
    result, strategy = _run()
    tx = result.transactions

    def on(day: str, action: str, tick: str | None = None):
        # raw runner transactions carry Timestamps (the API serializer ISO-formats them)
        return [t for t in tx if str(t["date"])[:10] == day and t["action"] == action
                and (tick is None or t["ticker"].startswith(tick))]

    # Cycle 1 entry: 4 stock shorts + 2 hedge longs, all expiring 2024-03-28.
    entry_shorts = on("2024-03-01", "SHORT")
    assert len(entry_shorts) == 4
    assert all("2024-03-28" in t["ticker"] for t in entry_shorts)
    assert len(on("2024-03-01", "BUY", "NIFTY|")) == 2

    # Flip 1 (Mar 5): the breached CE covers (reason=flip) and a fresh ATM PE is sold.
    assert [t["ticker"] for t in on("2024-03-05", "SHORT")] == ["RELIANCE|2024-03-28|1050|PE"]
    f1 = on("2024-03-05", "COVER", "RELIANCE|")
    assert {t["ticker"] for t in f1} == {"RELIANCE|2024-03-28|1050|CE",
                                         "RELIANCE|2024-03-28|950|PE"}
    assert all(t["exit_reason"] == "flip" for t in f1)
    # Flip 2 (Mar 7): PE 1050 rolls to CE 1000. Nothing on Mar 6/8 (no breach / once-a-day).
    assert not on("2024-03-06", "COVER") and not on("2024-03-08", "COVER")
    assert [t["ticker"] for t in on("2024-03-07", "SHORT")] == ["RELIANCE|2024-03-28|1000|CE"]
    # Flip 3 (Mar 11): max_flips reached — close the name, sell NOTHING new.
    assert on("2024-03-11", "COVER", "RELIANCE|") and not on("2024-03-11", "SHORT")
    assert "RELIANCE" in strategy.closed_names or strategy.flip_count == {}  # reset after cycle
    # Wild bars after the close-out must not trade RELIANCE again this cycle.
    assert not [t for t in tx if t["ticker"].startswith("RELIANCE|2024-03-28")
                and str(t["date"])[:10] > "2024-03-11"]

    # Expiry (Mar 28): INFY's two shorts + both hedge legs settle to intrinsic.
    settles = on("2024-03-28", "SETTLE")
    assert {t["ticker"] for t in settles} == {
        "INFY|2024-03-28|1550|CE", "INFY|2024-03-28|1450|PE",
        "NIFTY|2024-03-28|23000|CE", "NIFTY|2024-03-28|21000|PE",
    }

    # Re-entry (Mar 29): a fresh basket for the April expiry — state reset carried over.
    re_shorts = on("2024-03-29", "SHORT")
    assert re_shorts and all("2024-04-25" in t["ticker"] for t in re_shorts)


def test_estimate_capital_from_peak_margin():
    from skas_algo.services.donchian_bt import estimate_capital

    sched = _schedule(FakeSD())
    cap = estimate_capital(sched)
    # Modelled short margin per cycle: 4 stock legs (RELIANCE 2 × ~₹5L notional, INFY 2 ×
    # ~₹6L) at the flat SPAN-ish % — the exact value tracks MarginParams, so assert the
    # invariants: positive, lakh-rounded, ≥ the raw peak, and ~10% headroom.
    from skas_algo.engine.options.margin import MarginParams, short_option_margin

    peak = max(
        sum(short_option_margin(leg["spot"], leg["lot_size"] * leg["lots"], 1, MarginParams())
            for leg in c["legs"] if leg["side"] == "sell")
        for c in sched
    )
    assert cap is not None and cap % 100_000 == 0
    assert peak * 1.10 <= cap < peak * 1.10 + 100_000
    assert estimate_capital([]) is None


def test_basket_cycles_report_drilldown():
    from skas_algo.services.donchian_bt import basket_cycles_report

    result, _strategy = _run()
    # The report consumes the SERIALIZED trade log (ISO date strings), like the API does.
    trades = []
    for t in result.transactions:
        row = dict(t)
        row["date"] = str(t["date"])[:10]
        trades.append(row)
    history = [{"date": str(h["date"])[:10], "margin_used": h.get("margin_used")}
               for h in result.history]

    cycles = basket_cycles_report(trades, history)
    assert [c["cycle"] for c in cycles] == ["2024-03", "2024-04"]
    c1 = cycles[0]
    assert c1["entry_date"] == "2024-03-01" and c1["exit_date"] == "2024-03-28"
    assert c1["names"] == 2 and c1["exit_reason"] == "expiry"
    assert c1["flips"] >= 3  # RELIANCE's three breach events (each covers 1-2 legs)
    assert c1["margin_peak"] and c1["margin_peak"] > 0
    assert c1["pnl_net"] == pytest.approx(c1["pnl"] - c1["charges"])

    by_name = {n["name"]: n for n in c1["name_rows"]}
    rel = by_name["RELIANCE"]
    # notional default (₹7.5L / ₹5L-per-lot) sizes RELIANCE at 2 lots
    assert rel["side"] == "short" and rel["lot_size"] == 500 and rel["lots"] == 2
    assert rel["premium"] > 0 and rel["flips"] >= 3
    # Every RELIANCE leg is fully paired: entry price/date + exit price/date + reason.
    assert all(leg["entry_price"] is not None and leg["exit_price"] is not None
               for leg in rel["legs"])
    assert {leg["exit_reason"] for leg in rel["legs"]} <= {"flip", "expiry"}
    hedge = by_name["NIFTY"]
    assert hedge["side"] == "hedge" and hedge["lot_size"] == 50
    assert all(leg["side"] == "buy" for leg in hedge["legs"])
    infy = by_name["INFY"]
    assert all(leg["exit_reason"] == "expiry" for leg in infy["legs"])  # settled, no flips


def test_resolve_basket_exclude_include():
    from skas_algo.services.donchian_bt import resolve_basket

    avail = {"HDFCBANK", "RELIANCE", "ICICIBANK", "INFY", "TCS", "DIXON"}
    base = resolve_basket("nifty25", avail)
    assert base == ["HDFCBANK", "RELIANCE", "ICICIBANK", "INFY", "TCS"]  # cache-intersected
    # Exclude drops (case/space-insensitive); include appends cached names only; a
    # re-included excluded name comes back; an uncached include is ignored.
    got = resolve_basket("nifty25", avail,
                         exclude=[" reliance ", "TCS", "NOTINBASKET"],
                         include=["DIXON", "tcs", "GHOST", "DIXON"])
    assert got == ["HDFCBANK", "ICICIBANK", "INFY", "DIXON", "TCS"]


def test_notional_sizing_and_filters():
    from skas_algo.services.donchian_bt import build_cycle_schedule

    sd = FakeSD()
    # Notional sizing: RELIANCE ₹1000×500=₹5L/lot → 2 lots at ₹7.5L... round(1.5)=2;
    # INFY ₹1500×400=₹6L/lot → round(1.25)=1.
    cycles = build_cycle_schedule(sd, ["RELIANCE", "INFY"], START, END,
                                  skip_leg_min_premium_pct=0.0, notional_per_name=750_000)
    by = {(leg["underlying"], leg["right"]): leg for leg in cycles[0]["legs"]}
    assert by[("RELIANCE", "CE")]["lots"] == 2
    assert by[("INFY", "CE")]["lots"] == 1
    # A tiny target rounds every name to 0 lots → all names sit out.
    none = build_cycle_schedule(sd, ["RELIANCE", "INFY"], START, END,
                                skip_leg_min_premium_pct=0.0, notional_per_name=100_000)
    assert all(not c["legs"] for c in none)
    # Tight-channel gate: RELIANCE width (1050-950)/1000 = 10%, INFY (1550-1450)/1500 ≈ 6.7%
    # → a 8% floor keeps RELIANCE, drops INFY.
    gated = build_cycle_schedule(sd, ["RELIANCE", "INFY"], START, END,
                                 skip_leg_min_premium_pct=0.0, min_channel_width_pct=8.0)
    names = {leg["underlying"] for leg in gated[0]["legs"] if leg["side"] == "sell"}
    assert names == {"RELIANCE"}


def test_vix_rules():
    from skas_algo.services.donchian_bt import build_cycle_schedule

    class VixSD(FakeSD):
        def __init__(self, level):
            super().__init__()
            self.frames["INDIA VIX"] = pd.DataFrame({
                "date": CAL, "high": [level] * len(CAL), "low": [level] * len(CAL),
                "close": [float(level)] * len(CAL),
            })

    calm = build_cycle_schedule(VixSD(14), ["RELIANCE"], START, END,
                                skip_leg_min_premium_pct=0.0, notional_per_name=750_000,
                                vix_half_threshold=20)
    hot = build_cycle_schedule(VixSD(25), ["RELIANCE"], START, END,
                               skip_leg_min_premium_pct=0.0, notional_per_name=750_000,
                               vix_half_threshold=20)
    assert calm[0]["legs"][0]["lots"] == 2
    assert hot[0]["legs"][0]["lots"] == 1  # halved target → round(0.75) = 1
    skipped = build_cycle_schedule(VixSD(25), ["RELIANCE"], START, END,
                                   skip_leg_min_premium_pct=0.0,
                                   vix_skip_threshold=20)
    assert all(not c["legs"] for c in skipped)  # whole cycles skipped, cycle rows kept
    assert len(skipped) == 2
