"""The two-cadence decision model (ExitCadenceMixin) across the intraday options family.

Owner design 2026-07-18: every options strategy samples its profit/adjust decision on
`profit_check` and its stop on `stop_check`. Three properties are load-bearing:
  * the ctor default "tick" is EXACTLY the pre-cadence behavior (a recovered live deploy
    rebuilds byte-identical — §1);
  * `_due` consumes its window, so it must be sampled AFTER the readiness guards — a
    margin-pending or missing-print slice must NOT eat an evaluation slot;
  * hard time exits are never cadence-gated.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from skas_algo.strategies.call_put_ratio_expiry import CallPutRatioExpiryStrategy
from skas_algo.strategies.delta_neutral_monthly import DeltaNeutralMonthlyStrategy
from skas_algo.strategies.intraday_straddle import IntradayStraddleStrategy
from skas_algo.strategies.weekly_intraday_straddle import WeeklyIntradayStraddle

T0 = datetime(2026, 7, 16, 10, 0)


class _Mkt:
    def __init__(self):
        self.prices: dict[str, float] = {}

    def has_print(self, s):
        return s in self.prices


class _Ctx:
    def __init__(self):
        self.market = _Mkt()
        self._now = T0

    def now(self):
        return self._now

    def today(self):
        return self._now.date()

    def close(self, s):
        return self.market.prices[s]

    def lots(self, s):
        return 1


def _straddle(**kw):
    st = IntradayStraddleStrategy(universe=["NIFTY"], **kw)
    st.legs = [{"symbol": "NIFTY|2026-07-21|24100|CE", "dir": -1, "units": 65.0, "entry": 100.0},
               {"symbol": "NIFTY|2026-07-21|24100|PE", "dir": -1, "units": 65.0, "entry": 100.0}]
    st.set_broker_margin(200_000)
    ctx = _Ctx()
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"]
    return st, ctx


def _set_loss_pct(st, ctx, pct):
    """Marks such that MTM = -pct% of the 2L base (shorts: price UP = loss)."""
    per_unit = (200_000 * pct / 100.0) / (2 * 65.0)
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"] + per_unit


def test_tick_default_is_pre_cadence_behavior():
    """Default "tick": the stop fires on the very slice the loss crosses — as always."""
    st, ctx = _straddle()
    assert st.profit_check == "tick" and st.stop_check == "tick"
    _set_loss_pct(st, ctx, 2.5)                       # beyond the 2% default stop
    sigs = st._manage(ctx, st.legs, T0 + timedelta(seconds=30))
    assert len(sigs) == 2 and sigs[0].reason == "stop"


def test_stop_cadence_samples_on_the_interval():
    """stop_check=5min: evaluated at t0, then not again until t0+5m — a loss crossing in
    between waits for the next sample (the sampled-stop semantics, made explicit)."""
    st, ctx = _straddle(stop_check="5min")
    st._manage(ctx, st.legs, T0)                      # consumes the first stop window (flat pnl)
    _set_loss_pct(st, ctx, 2.5)
    assert st._manage(ctx, st.legs, T0 + timedelta(minutes=1)) == []   # in-window → no eval
    assert st._manage(ctx, st.legs, T0 + timedelta(minutes=4)) == []
    sigs = st._manage(ctx, st.legs, T0 + timedelta(minutes=5))
    assert len(sigs) == 2 and sigs[0].reason == "stop"


def test_pending_margin_does_not_consume_the_window():
    """A margin-pending early-return must not eat the cadence slot: the FIRST evaluated
    slice after margin arrives still samples immediately (mixin rule #1)."""
    st, ctx = _straddle(stop_check="5min")
    st.margin_source = ""                              # back to pending
    st._broker_margin = None
    _set_loss_pct(st, ctx, 2.5)
    assert st._manage(ctx, st.legs, T0) == []          # waits on margin — window intact
    assert st._manage(ctx, st.legs, T0 + timedelta(minutes=1)) == []
    st.set_broker_margin(200_000)                      # margin lands at t0+2m
    sigs = st._manage(ctx, st.legs, T0 + timedelta(minutes=2))
    assert len(sigs) == 2 and sigs[0].reason == "stop"  # first sample fires — not swallowed


def test_hard_time_exit_is_never_gated():
    """Even with stop_check=60min mid-window, 15:25 squares off."""
    st, ctx = _straddle(stop_check="60min")
    st._manage(ctx, st.legs, T0)                       # consume the stop window
    sigs = st._manage(ctx, st.legs, datetime(2026, 7, 16, 15, 25))
    assert len(sigs) == 2 and sigs[0].reason == "eod"


def test_trail_peak_rides_the_profit_cadence():
    """profit_check=5min: the high-water mark only updates on profit samples."""
    st, ctx = _straddle(profit_check="5min")
    st._manage(ctx, st.legs, T0)                       # consume profit window at t0 (pnl 0)
    # profit of +1.5% (shorts: price DOWN = gain)
    per_unit = (200_000 * 1.5 / 100.0) / (2 * 65.0)
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"] - per_unit
    st._manage(ctx, st.legs, T0 + timedelta(minutes=2))
    assert st.peak_pct == 0.0                          # in-window → peak not sampled
    st._manage(ctx, st.legs, T0 + timedelta(minutes=5))
    assert st.peak_pct > 1.4                           # sampled on the interval


def test_cpre_underlyings_have_independent_clocks():
    """NIFTY's stop sample must not consume SENSEX's window (per-book cadence keys)."""
    st = CallPutRatioExpiryStrategy(universe=["NIFTY"], underlyings=["NIFTY", "SENSEX"],
                                    stop_check="5min", profit_check="5min")
    ctx = _Ctx()
    legs = {}
    for u, strike in (("NIFTY", 24100), ("SENSEX", 80000)):
        legs[u] = [{"symbol": f"{u}|2026-07-21|{strike}|CE", "dir": -1, "units": 65.0, "entry": 100.0}]
        st.legs[u] = legs[u]
        ctx.market.prices[legs[u][0]["symbol"]] = 100.0
        st.margin_base[u] = 200_000.0
        st.margin_source[u] = "broker"
    st._manage(ctx, "NIFTY", legs["NIFTY"], T0)        # consumes NIFTY's window only
    # SENSEX blows through its stop one minute later — must evaluate despite NIFTY's sample
    ctx.market.prices[legs["SENSEX"][0]["symbol"]] = 100.0 + (200_000 * 1.5 / 100.0) / 65.0
    sigs = st._manage(ctx, "SENSEX", legs["SENSEX"], T0 + timedelta(minutes=1))
    assert sigs and sigs[0].reason == "stop"


def test_delta_neutral_adjustments_ride_profit_cadence():
    """profit_check=5min gates the adjustment dispatch too (they share the cadence);
    the stop keeps its own clock."""
    st = DeltaNeutralMonthlyStrategy(universe=["BANKNIFTY"], profit_check="5min",
                                     stop_loss_pct=1.0, stop_check="tick")
    calls = []
    st._maybe_adjust = lambda ctx, live, marks, now: calls.append(now) or []
    st.phase = "strangle"
    st.legs = [{"symbol": "BANKNIFTY|2026-07-30|52000|CE", "right": "CE", "dir": -1,
                "units": 30.0, "entry": 300.0},
               {"symbol": "BANKNIFTY|2026-07-30|51000|PE", "right": "PE", "dir": -1,
                "units": 30.0, "entry": 300.0}]
    st.set_broker_margin(200_000)
    ctx = _Ctx()
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"]
    st._manage(ctx, st.legs, T0)                       # profit window consumed → adjust ran
    assert len(calls) == 1
    st._manage(ctx, st.legs, T0 + timedelta(minutes=2))
    assert len(calls) == 1                             # in-window → adjustment gated
    st._manage(ctx, st.legs, T0 + timedelta(minutes=5))
    assert len(calls) == 2                             # next sample


def test_weekly_stop_gated_and_vwap_untouched():
    st = WeeklyIntradayStraddle(universe=["NIFTY"], stop_loss_pct=2.0, stop_check="5min")
    st.legs = [{"symbol": "NIFTY|2026-07-21|24100|CE", "dir": -1, "units": 65.0, "entry": 100.0},
               {"symbol": "NIFTY|2026-07-21|24100|PE", "dir": -1, "units": 65.0, "entry": 100.0}]
    st.set_broker_margin(200_000)
    ctx = _Ctx()
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"]
    st._protective_exit(ctx, st.legs, T0)              # consume window
    per_unit = (200_000 * 2.5 / 100.0) / (2 * 65.0)
    for leg in st.legs:
        ctx.market.prices[leg["symbol"]] = leg["entry"] + per_unit
    assert st._protective_exit(ctx, st.legs, T0 + timedelta(minutes=1)) == []
    sigs = st._protective_exit(ctx, st.legs, T0 + timedelta(minutes=5))
    assert sigs and sigs[0].reason == "stop"


# --------------------------------------------------------------- the opening five minutes
# OWNER RULE 2026-09-06: no option strategy books a profit or a stop in the first minutes of
# the session. At 09:15 an index option's spread is 3-7% of mid (0.3% by 09:30) and many LTPs
# are still yesterday's close, so a %-rule measured there is measured on fiction — and the
# exit would pay that spread twice over. Paper run 30 booked a "+3% target" that realised
# −₹9,765 doing exactly this.

OPEN_HELD = datetime(2026, 7, 16, 9, 16)
OPEN_OK = datetime(2026, 7, 16, 9, 20)


def test_a_stop_in_the_first_five_minutes_waits_for_0920():
    """The loss is real and beyond the 2% stop at 09:16 — the exit still waits."""
    st, ctx = _straddle()                                  # stop_check="tick"
    _set_loss_pct(st, ctx, 2.5)
    assert st._manage(ctx, st.legs, OPEN_HELD) == []
    assert st._manage(ctx, st.legs, datetime(2026, 7, 16, 9, 19, 59)) == []
    sigs = st._manage(ctx, st.legs, OPEN_OK)
    assert len(sigs) == 2 and sigs[0].reason == "stop"


def test_the_profit_side_is_held_too_the_trail_does_not_ratchet_on_open_prices():
    """The profit cadence is gated by the same rule, so the trail's high-water does not
    ratchet off an opening print — which would drag the stop up to a level the real book
    never reached, and fire it minutes later on a settled price."""
    st, ctx = _straddle()                                  # profit_check="tick"
    _set_loss_pct(st, ctx, -1.5)                           # +1.5% (shorts decayed)
    st._manage(ctx, st.legs, OPEN_HELD)
    assert st.peak_pct == 0.0                              # held → no high-water off 09:16
    st._manage(ctx, st.legs, OPEN_OK)
    assert st.peak_pct > 1.4                               # sampled once the open has settled


def test_the_held_open_does_not_consume_the_cadence_window():
    """A held slice must not eat an evaluation slot: with a 5-min cadence the FIRST sample
    at 09:20 fires at once, rather than waiting five minutes from a window it never used."""
    st, ctx = _straddle(stop_check="5min")
    _set_loss_pct(st, ctx, 2.5)
    for m in (15, 16, 17, 18, 19):
        assert st._manage(ctx, st.legs, datetime(2026, 7, 16, 9, m)) == []
    sigs = st._manage(ctx, st.legs, OPEN_OK)
    assert len(sigs) == 2 and sigs[0].reason == "stop"


def test_the_eod_backtest_slice_is_untouched_by_the_guard():
    """Mode equivalence (§3): the EOD engine has no clock on its ctx, so `_now` reads 15:30
    and the guard is a no-op — an EOD backtest is byte-identical to before."""
    st, ctx = _straddle()
    _set_loss_pct(st, ctx, 2.5)

    class _Dateless:                                       # the stub ctx shape: today(), no now()
        market = ctx.market
        def today(self):                                   # noqa: E301
            return OPEN_HELD.date()

    assert st._open_settled(st._now(_Dateless())) is True


def test_the_guard_releases_exactly_at_its_floor_and_survives_a_bad_value():
    from skas_algo.strategies._options_common import EXIT_NOT_BEFORE, OpenSettleGuard

    g = OpenSettleGuard()
    assert EXIT_NOT_BEFORE.strftime("%H:%M") == "09:20"
    assert g._open_settled(OPEN_HELD) is False
    assert g._open_settled(OPEN_OK) is True
    g.exit_not_before = "09:30"                            # a strategy may hold longer
    assert g._open_settled(OPEN_OK) is False
    g.exit_not_before = "not a time"                       # malformed → the platform floor,
    assert g._open_settled(OPEN_HELD) is False             # never a wider window
    assert g._open_settled(OPEN_OK) is True


def test_the_manual_builder_and_the_donchian_basket_hold_the_open_too():
    """Both exit OUTSIDE the cadence mixin (leg target/stop, spot bands, breach flips), so
    they carry the guard themselves. Housekeeping above it still runs."""
    from skas_algo.strategies._options_common import OpenSettleGuard
    from skas_algo.strategies.custom_options import CustomOptionsStrategy
    from skas_algo.strategies.donchian_strangle_monthly import DonchianStrangleMonthlyStrategy

    held = _Ctx()
    held._now = OPEN_HELD
    for cls in (CustomOptionsStrategy, DonchianStrangleMonthlyStrategy):
        assert issubclass(cls, OpenSettleGuard)
        st = object.__new__(cls)
        st.legs = ["NIFTY|2026-07-21|24100|CE"]
        st.done = False
        args = (held, None, OPEN_HELD.date()) if cls is CustomOptionsStrategy else (held,)
        assert st._manage(*args) == []
        assert st.done is False                            # a held tick is not a finished run
        # …and with NO open legs the one-shot housekeeping still runs, guard or no guard.
        st.legs = []
        assert st._manage(*args) == [] and st.done is True


def test_the_exit_rules_text_says_the_open_is_held():
    st, _ = _straddle()
    assert "never before 09:20" in st._cadence_phrase("stop")
