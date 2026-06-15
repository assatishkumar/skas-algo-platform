"""Options engine primitives: instrument encoding, short lots, settlement, margin, executor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from skas_algo.db.enums import OrderSide
from skas_algo.engine.execution import SliceExecutor
from skas_algo.engine.options import instrument as inst
from skas_algo.engine.options.margin import MarginModel, MarginParams, short_option_margin
from skas_algo.engine.options.settlement import ExpirySettler
from skas_algo.engine.overrides import CloseShort, OpenShort, OverrideResolver
from skas_algo.engine.portfolio import Portfolio
from skas_algo.engine.stops import StopBook

EXPIRY = date(2024, 1, 25)
CE = f"NIFTY|2024-01-25|21000|CE"
PE = f"NIFTY|2024-01-25|21000|PE"


# ------------------------------------------------------------------ instrument
def test_instrument_encode_parse_roundtrip():
    i = inst.make("NIFTY", EXPIRY, 21000, "CE", lot_size=75)
    assert i.symbol == CE
    back = inst.parse(CE)
    assert back is not None
    assert back.underlying == "NIFTY" and back.expiry == EXPIRY
    assert back.strike == 21000 and back.right == "CE"


def test_parse_returns_none_for_equity():
    assert inst.parse("RELIANCE") is None
    assert inst.is_option_symbol("RELIANCE") is False
    assert inst.is_option_symbol(CE) is True


# ------------------------------------------------------------------ portfolio shorts
def test_sell_to_open_and_buy_to_close():
    p = Portfolio(cash=1_000_000)
    lot = p.sell_to_open(CE, units=75, price=100.0, when=EXPIRY)  # receive premium
    assert lot.direction == -1
    assert p.cash == 1_000_000 + 75 * 100  # +7,500 premium

    # Mark-to-market: short is a liability — if price rises to 120, equity falls.
    assert p.holdings_value({CE: 120.0}) == -75 * 120
    # Buy back at 60 -> profit (100-60)*75 = 3,000
    profit = p.buy_to_close(CE, lot.id, 60.0)
    assert profit == (100 - 60) * 75
    assert p.cash == 1_000_000 + 75 * 100 - 75 * 60
    assert p.lots(CE) == []


def test_short_lot_state_roundtrip():
    p = Portfolio(cash=500_000)
    p.sell_to_open(CE, units=75, price=90.0, when=EXPIRY, multiplier=1)
    state = p.export_state()
    p2 = Portfolio(cash=0)
    p2.load_state(state)
    lots = p2.lots(CE)
    assert len(lots) == 1 and lots[0].direction == -1 and lots[0].price == 90.0


def test_long_path_unchanged_by_new_fields():
    # A long equity lot still values as units*close (direction/multiplier defaults).
    p = Portfolio(cash=100_000)
    p.buy("RELIANCE", units=10, price=2500.0, when=EXPIRY)
    assert p.holdings_value({"RELIANCE": 2600.0}) == 10 * 2600
    assert p.invested_capital() == 10 * 2500


# ------------------------------------------------------------------ settlement
def test_expiry_settlement_short_call_loss_and_put_expires_worthless():
    p = Portfolio(cash=1_000_000)
    ce_lot = p.sell_to_open(CE, units=75, price=100.0, when=date(2024, 1, 1))
    pe_lot = p.sell_to_open(PE, units=75, price=80.0, when=date(2024, 1, 1))

    # Spot finishes at 21300: CE intrinsic 300 (loss), PE intrinsic 0 (full premium kept).
    settler = ExpirySettler(spot_provider=lambda u, d: 21300.0)
    events = settler.settle(EXPIRY, p)

    by_sym = {e["ticker"]: e for e in events}
    assert all(e["action"] == "SETTLE" for e in events)
    assert by_sym[CE]["profit"] == (100 - 300) * 75    # -15,000
    assert by_sym[PE]["profit"] == (80 - 0) * 75        # +6,000
    assert p.lots(CE) == [] and p.lots(PE) == []        # lots removed


def test_settlement_ignores_non_expired_and_equity():
    p = Portfolio(cash=1_000_000)
    p.sell_to_open(CE, units=75, price=100.0, when=date(2024, 1, 1))
    p.buy("RELIANCE", units=10, price=2500.0, when=date(2024, 1, 1))
    # A date before expiry settles nothing.
    settler = ExpirySettler(spot_provider=lambda u, d: 21300.0)
    assert settler.settle(date(2024, 1, 24), p) == []
    assert len(p.lots(CE)) == 1


# ------------------------------------------------------------------ margin
def test_margin_math_and_sizing():
    p = MarginParams(span_pct=0.10, exposure_pct=0.03)
    # notional = 21000*75 = 1,575,000 ; margin = 0.13 * notional
    assert short_option_margin(21000, 75, 1, p) == 0.13 * 21000 * 75

    model = MarginModel(spot_provider=lambda u, d: 21000.0, params=p)
    port = Portfolio(cash=5_000_000)
    port.sell_to_open(CE, units=75, price=100.0, when=EXPIRY)
    used = model.margin_used(port, EXPIRY)
    assert used == 0.13 * 21000 * 75
    assert model.max_margin_used == used
    # lots affordable within 80% of 5,000,000 capital
    n = model.lots_affordable(21000, 75, 1, capital=5_000_000, utilization=0.80)
    assert n == int((5_000_000 * 0.80) // (0.13 * 21000 * 75))


# ------------------------------------------------------------------ executor
@dataclass
class _Fill:
    price: float


class _StubBroker:
    """Fills at a per-symbol reference price set by the test."""

    def __init__(self, prices):
        self.prices = prices

    def execute(self, order):
        return _Fill(self.prices[order.symbol])


def _executor(prices):
    return SliceExecutor(Portfolio(cash=2_000_000), StopBook(), OverrideResolver(), _StubBroker(prices))


def test_executor_open_and_close_short_events():
    ex = _executor({CE: 100.0})
    open_ev = ex._execute(EXPIRY, OpenShort(CE, units=75, multiplier=1), {})
    assert open_ev[0]["action"] == "SHORT" and open_ev[0]["price"] == 100.0
    assert ex.portfolio.cash == 2_000_000 + 75 * 100

    lot = ex.portfolio.lots(CE)[0]
    ex.broker.prices[CE] = 40.0  # buy back cheaper -> profit
    close_ev = ex._execute(EXPIRY, CloseShort(CE, lot.id), {})
    assert close_ev[0]["action"] == "COVER"
    assert close_ev[0]["profit"] == (100 - 40) * 75
    assert ex.portfolio.lots(CE) == []


def test_nifty_lot_size_history():
    """SEBI lot-size revisions (user-confirmed 2026-06) are picked by date."""
    from skas_algo.engine.options.contract_specs import lot_size_for

    assert lot_size_for("NIFTY", date(2023, 6, 15)) == 50
    assert lot_size_for("NIFTY", date(2024, 4, 25)) == 50   # last day of 50
    assert lot_size_for("NIFTY", date(2024, 4, 26)) == 25   # halved
    assert lot_size_for("NIFTY", date(2024, 11, 19)) == 25
    assert lot_size_for("NIFTY", date(2024, 11, 20)) == 75  # SEBI ₹15L floor
    assert lot_size_for("NIFTY", date(2025, 12, 30)) == 75
    assert lot_size_for("NIFTY", date(2026, 1, 1)) == 65    # reduced again


def test_nifty_expiry_weekday_history():
    """Thursday for years; SEBI moved NSE expiries to Tuesday from 2025-09-01."""
    from skas_algo.engine.options.contract_specs import (
        expected_monthly_expiry,
        expiry_weekday_for,
    )

    assert expiry_weekday_for("NIFTY", date(2024, 6, 1), "monthly") == 3   # Thursday
    assert expiry_weekday_for("NIFTY", date(2025, 9, 1), "monthly") == 1   # Tuesday
    assert expiry_weekday_for("BANKNIFTY", date(2024, 1, 1), "weekly") == 2  # Wednesday era
    assert expiry_weekday_for("BANKNIFTY", date(2025, 1, 1), "weekly") is None  # discontinued
    # Last Thursday of June 2024 vs last Tuesday of October 2025.
    assert expected_monthly_expiry("NIFTY", 2024, 6) == date(2024, 6, 27)
    assert expected_monthly_expiry("NIFTY", 2025, 10) == date(2025, 10, 28)


# ------------------------------------------------------------------ equity fallback
def test_market_view_equity_loader_fallback():
    """A plain (non-option) symbol inside an options run is priced via the optional
    equity_loader; without it the lazy view has no series and close() raises."""
    import pandas as pd

    from skas_algo.engine.options.market import OptionMarketView

    cal = [date(2024, 1, 22), date(2024, 1, 23)]
    etf = pd.DataFrame({"date": cal, "close": [60.0, 61.0]})

    def opt_loader(symbol, start, end):
        return None  # options loader can't decode a plain symbol

    def equity_loader(symbol, start, end):
        return etf if symbol == "GOLDBEES" else None

    mv = OptionMarketView(opt_loader, chain=None, calendar=cal, equity_loader=equity_loader)
    mv.set_date(pd.Timestamp(cal[1]))
    assert mv.close("GOLDBEES") == 61.0
    assert mv.has_print("GOLDBEES") is True

    bare = OptionMarketView(opt_loader, chain=None, calendar=cal)  # no fallback
    bare.set_date(pd.Timestamp(cal[1]))
    try:
        bare.close("GOLDBEES")
        raise AssertionError("expected KeyError without equity_loader")
    except KeyError:
        pass
