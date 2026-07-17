"""broker_smoke_test: premium-band strike pick, 1-lot/1-share hard sizing, timed exit,
self-stop lifecycle, state round-trip, and the manager's self-stop seam (flat-book guard).
Fake market/chain only — per §1 the real cycle is the owner's hand."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

from skas_algo.strategies.broker_smoke_test import BrokerSmokeTestStrategy

WEEKLY = date(2026, 7, 21)
T0 = datetime(2026, 7, 17, 10, 0)


def chain(rows=None, lot=65):
    if rows is None:
        # premiums straddling the ₹5–20 band; 24700 (₹9.8) is nearest the ₹10 target
        rows = [
            {"strike": 24500.0, "ce": {"ltp": 45.0, "oi": 9000}, "pe": {"ltp": 30.0, "oi": 9000}},
            {"strike": 24600.0, "ce": {"ltp": 18.5, "oi": 9000}, "pe": {"ltp": 12.0, "oi": 9000}},
            {"strike": 24700.0, "ce": {"ltp": 9.8, "oi": 9000}, "pe": {"ltp": 6.0, "oi": 9000}},
            {"strike": 24800.0, "ce": {"ltp": 5.2, "oi": 40}, "pe": {"ltp": 3.0, "oi": 9000}},
            {"strike": 24900.0, "ce": {"ltp": 2.1, "oi": 9000}, "pe": {"ltp": 1.0, "oi": 9000}},
        ]
    return {"spot": 24200.0, "lot_size": lot, "rows": rows}


class FakeCacheChain:
    def expiries(self, _u, today):
        return [e for e in [WEEKLY] if e >= today]


class FakeMarket:
    def __init__(self, chain_dict):
        self.chain_dict = chain_dict
        self.prices: dict[str, float] = {}

    def live_chain(self, _u, _e):
        return self.chain_dict


class FakeCtx:
    def __init__(self, market):
        self.market = market
        self._now = None
        self.positions: dict[str, float] = {}

    def now(self):
        return self._now

    def today(self):
        return self._now.date()

    def option_chain(self):
        return FakeCacheChain()

    def lots(self, s):
        return self.positions.get(s, 0)

    def close(self, s):
        if s in self.market.prices:
            return self.market.prices[s]
        raise KeyError(s)


def tick(st, ctx, dt):
    ctx._now = dt
    return st.on_slice(ctx)


def _fill(st, ctx):
    for leg in st.legs:
        ctx.positions[leg["symbol"]] = leg["units"]


def test_option_entry_picks_the_band_strike_one_lot():
    st = BrokerSmokeTestStrategy(leg="option", underlying="NIFTY", right="CE")
    ctx = FakeCtx(FakeMarket(chain()))
    sigs = tick(st, ctx, T0)
    assert len(sigs) == 1 and sigs[0].action.name == "ENTER_LONG"
    # nearest ₹10 inside ₹5–20: the ₹9.8 strike (₹18.5 is further; ₹5.2 fails the OI floor)
    assert "24700" in sigs[0].symbol and sigs[0].quantity == 65   # exactly ONE lot
    assert st.strategy_alert is None and st.entry_at == T0


def test_option_entry_respects_band_and_oi_and_alerts_when_empty():
    rows = [{"strike": 24800.0, "ce": {"ltp": 5.2, "oi": 40}, "pe": {"ltp": 3.0, "oi": 40}},
            {"strike": 24500.0, "ce": {"ltp": 45.0, "oi": 9000}, "pe": {"ltp": 30.0, "oi": 9000}}]
    st = BrokerSmokeTestStrategy(leg="option")
    ctx = FakeCtx(FakeMarket(chain(rows)))
    assert tick(st, ctx, T0) == []                                # nothing eligible → no entry
    assert st.strategy_alert and "premium" in st.strategy_alert  # says WHY, keeps retrying


def test_timed_exit_then_self_stop():
    st = BrokerSmokeTestStrategy(leg="option", hold_seconds=60)
    ctx = FakeCtx(FakeMarket(chain()))
    tick(st, ctx, T0)
    _fill(st, ctx)
    assert tick(st, ctx, T0 + timedelta(seconds=30)) == []        # still holding
    sigs = tick(st, ctx, T0 + timedelta(seconds=61))              # hold elapsed → sell
    assert len(sigs) == 1 and sigs[0].action.name == "EXIT_ALL" and sigs[0].reason == "smoke_exit"
    assert st.stop_requested is False                             # not until the book is flat
    assert tick(st, ctx, T0 + timedelta(seconds=71)) == []        # flat again → done
    assert st.stop_requested is True
    assert tick(st, ctx, T0 + timedelta(seconds=81)) == []        # stays inert afterwards


def test_stock_leg_buys_one_share():
    st = BrokerSmokeTestStrategy(leg="stock", symbol="ITC", hold_seconds=60)
    ctx = FakeCtx(FakeMarket(chain()))
    ctx.market.prices["ITC"] = 452.5
    sigs = tick(st, ctx, T0)
    assert len(sigs) == 1 and sigs[0].symbol == "ITC" and sigs[0].quantity == 1
    _fill(st, ctx)
    sigs = tick(st, ctx, T0 + timedelta(seconds=61))
    assert len(sigs) == 1 and sigs[0].action.name == "EXIT_ALL"
    tick(st, ctx, T0 + timedelta(seconds=71))
    assert st.stop_requested is True


def test_state_round_trip_mid_hold_still_exits():
    st = BrokerSmokeTestStrategy(leg="option", hold_seconds=60)
    ctx = FakeCtx(FakeMarket(chain()))
    tick(st, ctx, T0)
    _fill(st, ctx)

    st2 = BrokerSmokeTestStrategy(leg="option", hold_seconds=60)
    st2.load_state(st.export_state())                             # restart mid-hold
    assert st2.legs and st2.entry_at == T0
    sigs = tick(st2, ctx, T0 + timedelta(seconds=61))
    assert len(sigs) == 1 and sigs[0].action.name == "EXIT_ALL"   # recovered run still exits


def test_manager_self_stop_seam_guards_on_flat_book(monkeypatch):
    """The loop honors stop_requested ONLY on a flat book — a run holding positions must
    never abandon them, no matter what the strategy claims."""
    from skas_algo.live.manager import manager

    stopped = []
    monkeypatch.setattr(manager, "stop", lambda rid: stopped.append(rid))

    def live(requested, symbols):
        return SimpleNamespace(
            run_id=42,
            config=SimpleNamespace(name="smoke"),
            session=SimpleNamespace(
                strategy=SimpleNamespace(stop_requested=requested),
                portfolio=SimpleNamespace(lot_symbols=lambda: symbols),
            ),
        )

    assert manager._maybe_self_stop(live(False, [])) is False     # nothing requested
    assert manager._maybe_self_stop(live(True, ["X"])) is False   # holding → refuse
    assert stopped == []
    assert manager._maybe_self_stop(live(True, [])) is True       # flat + requested → stop
    assert stopped == [42]
