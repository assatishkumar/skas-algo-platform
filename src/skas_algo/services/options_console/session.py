"""One interactive console session: a minute cursor over the 1-min option store.

The batch replay (``services/intraday_replay``) sweeps a date range with a strategy in the
loop and hands back a report. This is the same market with a HUMAN in the loop: open a day,
step the clock, read the chain, and (later phases) trade it by hand.

**Seeking is rewind-and-replay, always.** ``seek()`` rebuilds the market from the day's open
and re-feeds prints up to the target minute rather than checkpointing. Three reasons, in
order of importance:

1. It is the only construction that reproduces forward-fill and the stale-print window
   exactly — a snapshot of ``quotes`` would have to reproduce *when* each mark was last
   seen, which is the very thing ``has_print`` reads.
2. It makes the design's backward jog chips (−1m, −1h, SOD) correct by construction rather
   than by a separate un-apply path.
3. It makes determinism testable as an IDENTITY: stepping 105 times must equal seeking once.

It is affordable because it is cheap: a whole 376-minute session rebuilds in ~0.07 s
(``load_day`` 0.31 s once per day, ~0.18 ms per minute). Measured on NIFTY 2026-04-01.

The consequence is worth stating rather than discovering: **rewinding past a fill unwinds
it.** Fills live in a journal stamped with their minute, and the book is whatever the
journal says at the cursor. That matches the design, which draws fills as markers on the
replay track.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from skas_algo.data.option_intraday_store import captured_days, load_day
from skas_algo.engine.options import black_scholes as bs
from skas_algo.services.replay_market import ReplayChain, ReplayMarket

# The session window the store is filtered to. 15:40 is the post-CAS close the store itself
# measures (last minute-bar 15:39); before 2026-08-03 nothing trades past 15:29, so an
# over-wide end is harmless and an over-tight one silently truncates.
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 40)

# One risk-free rate for the whole feature, shipped to the frontend in ConsoleState.pricing
# so the chain's IV/Δ and the payoff's Δ cannot come from two different numbers. Matches
# routes/data.py's DEFAULT_RISK_FREE and web/src/lib/payoff.ts's RISK_FREE.
RISK_FREE = 0.065
# Expiry is 15:30 and time-to-expiry is floored at 2 minutes — copied from
# engine/live.py::_enrich_greeks so replay and live greeks share one convention.
EXPIRY_TIME = time(15, 30)
T_FLOOR_S = 120.0
_YEAR_S = 365.0 * 24 * 3600

UNDERLYINGS = ("NIFTY", "BANKNIFTY", "SENSEX")


def _t_years(expiry_iso: str, now: datetime) -> float:
    exp = datetime.combine(date.fromisoformat(expiry_iso[:10]), EXPIRY_TIME)
    return max(T_FLOOR_S, (exp - now).total_seconds()) / _YEAR_S


@dataclass
class _Tape:
    """One day's prints, sorted once: minute string, symbol, close, oi as parallel lists.

    Same single-pass shape the batch replay uses (a pointer walks the lists as the clock
    advances) — Python lists index far faster than numpy scalars, and building per-minute
    dicts up front was 63% of that harness's runtime before it was profiled out."""

    day: date
    minutes: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    closes: list[float] = field(default_factory=list)
    ois: list[float] = field(default_factory=list)
    all_symbols: list[str] = field(default_factory=list)
    expiries: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, underlying: str, day: date) -> _Tape:
        df = load_day(day, underlying=underlying, columns=["symbol", "start", "close", "oi"])
        if df.empty:
            return cls(day=day)
        mins = pd.to_datetime(df["start"]).values.astype("datetime64[m]")
        order = np.argsort(mins, kind="stable")          # within-minute order preserved
        syms = df["symbol"].to_numpy()[order].tolist()
        uniq = sorted(set(syms))
        return cls(
            day=day,
            minutes=np.datetime_as_string(mins[order]).tolist(),
            symbols=syms,
            closes=df["close"].to_numpy()[order].astype(float).tolist(),
            ois=df["oi"].to_numpy()[order].astype(float).tolist(),
            all_symbols=uniq,
            expiries=sorted({s.split("|")[1] for s in uniq}),
        )


class ConsoleSession:
    """A console cursor. In-process and single-user, like everything else here (§7)."""

    def __init__(self, *, underlying: str = "NIFTY", day: date | None = None,
                 at: str | None = None, expiry: str | None = None,
                 capital: float = 500_000.0, strike_window: int = 20,
                 allow_fifty_strikes: bool = False,
                 margin_per_lot_set: float = 0.0) -> None:
        u = underlying.upper()
        if u not in UNDERLYINGS:
            raise ValueError(f"unknown underlying {underlying!r} — the 1-min store holds "
                             f"{', '.join(UNDERLYINGS)}")
        self.id = uuid.uuid4().hex[:12]
        self.underlying = u
        self.capital = float(capital)
        self.strike_window = max(4, int(strike_window))
        self.allow_fifty_strikes = bool(allow_fifty_strikes)
        self.margin_per_lot_set = float(margin_per_lot_set)
        self.created_at = datetime.now()

        self.days = self._replayable_days()
        if not self.days:
            raise ValueError(f"no captured days for {u} in the 1-min store")
        self.day = day or self.days[-1]
        if self.day not in self.days:
            raise ValueError(f"{self.day.isoformat()} is not in the store for {u}")

        self.market = ReplayMarket(u, allow_fifty_strikes=self.allow_fifty_strikes)
        self.chain_view = ReplayChain(self.market)
        self.tape = _Tape(day=self.day)
        self.expiry: str | None = expiry
        self.clock: datetime = datetime.combine(self.day, SESSION_OPEN)
        self._open_day(self.day)
        self.seek(at or SESSION_OPEN.strftime("%H:%M"))

    # ----------------------------------------------------------------- days / tape
    def _replayable_days(self) -> list[date]:
        return [date.fromisoformat(d) for d in captured_days()]

    def _open_day(self, day: date) -> None:
        self.day = day
        self.tape = _Tape.load(self.underlying, day)
        self.chain_view.days = sorted({date.fromisoformat(e) for e in self.tape.expiries})
        if self.expiry not in self.tape.expiries:
            # Default to the nearest expiry that is not already past — the design's chip row
            # opens on the one the eye lands on, and a settled expiry has nothing to trade.
            future = [e for e in self.tape.expiries if date.fromisoformat(e) >= day]
            self.expiry = (future or self.tape.expiries or [None])[0]

    def set_day(self, day: date) -> ConsoleSession:
        if day not in self.days:
            raise ValueError(f"{day.isoformat()} is not in the store for {self.underlying}")
        self._open_day(day)
        self.seek(SESSION_OPEN.strftime("%H:%M"))
        return self

    def shift_day(self, days: int) -> ConsoleSession:
        """±1d on the jog row: the NEXT captured day, not the next calendar day (a holiday
        or a capture gap would otherwise land on an empty session)."""
        i = self.days.index(self.day)
        return self.set_day(self.days[max(0, min(len(self.days) - 1, i + days))])

    # ----------------------------------------------------------------- the cursor
    def _rebuild_to(self, target: datetime) -> None:
        """Replay the day from its open up to ``target``. ONE pass over the tape: feed each
        print, and whenever the minute rolls over, sample the parity spot into today's
        FORMING O/H/L. The forming bar is why this cannot be a plain quote snapshot — the
        settled daily bar is lookahead (at 09:30 its high, low and close are all the
        future), so the market strip has to read the path the session actually took."""
        m = self.market
        m.start_day(self.day, self.tape.all_symbols)
        key = target.strftime("%Y-%m-%dT%H:%M")
        q = m.quotes
        mins, syms, closes, ois = (self.tape.minutes, self.tape.symbols,
                                   self.tape.closes, self.tape.ois)
        prev = None
        for i in range(len(mins)):
            minute = mins[i]
            if minute > key:
                break
            if minute != prev:
                if prev is not None:
                    m._spot_dirty = True
                    m.note_spot()      # close the minute that just ended
                prev = minute
            q[syms[i]] = (closes[i], ois[i], minute)
        m._spot_dirty = True
        if prev is not None:
            m.note_spot()              # and the minute the cursor sits in
        m._spot_dirty = True
        m.now = target
        self.clock = target

    def seek(self, at: str | datetime) -> ConsoleSession:
        if isinstance(at, str):
            hh, mm = (at.split(":") + ["0"])[:2]
            target = datetime.combine(self.day, time(int(hh), int(mm)))
        else:
            target = at
        lo = datetime.combine(self.day, SESSION_OPEN)
        hi = datetime.combine(self.day, SESSION_CLOSE)
        self._rebuild_to(max(lo, min(hi, target)))
        return self

    def step(self, minutes: int) -> ConsoleSession:
        return self.seek(self.clock + timedelta(minutes=int(minutes)))

    # ----------------------------------------------------------------- the chain
    def chain_rows(self) -> list[dict]:
        """The visible ladder, with IV and Δ solved off each leg's own LTP.

        Solved HERE rather than in the browser so the chain's Δ column and the payoff's Δ
        come from one calculator (0.16 ms for 44 strikes × 2 sides — cheaper than the minute
        step itself, so there is no reason to have two). A leg that has not printed inside
        the stale window is reported ``quoted: false`` with a null price: the store is trades
        only, and a BS-interpolated price would be a number the market never showed."""
        if not self.expiry:
            return []
        snap = self.market.live_chain(self.underlying, self.expiry)
        if not snap:
            return []
        spot, atm = snap["spot"], snap["atm_strike"]
        t = _t_years(self.expiry, self.clock)
        keep = [r for r in snap["rows"]
                if abs(r["strike"] - atm) <= self.strike_window * self._grid(snap["rows"])]

        def leg(cell: dict | None, right: str, strike: float) -> dict:
            if not cell or not cell.get("ltp"):
                return {"ltp": None, "oi": None, "iv": None, "delta": None,
                        "quoted": False, "stale_min": None}
            ltp = float(cell["ltp"])
            iv = bs.implied_vol(ltp, spot, strike, t, RISK_FREE, right)
            d = bs.delta(spot, strike, t, RISK_FREE, iv, right) if iv else None
            sym = f"{self.underlying}|{self.expiry}|{int(strike)}|{right}"
            q = self.market.quotes.get(sym)
            stale = None
            if q is not None:
                seen = datetime.fromisoformat(q[2])
                stale = int((self.clock - seen).total_seconds() // 60)
            return {"ltp": ltp, "oi": cell.get("oi"),
                    "iv": round(iv * 100, 2) if iv else None,
                    "delta": round(d, 4) if d is not None else None,
                    "quoted": True, "stale_min": stale}

        out = []
        for r in keep:
            k = r["strike"]
            ce, pe = leg(r.get("ce"), "CE", k), leg(r.get("pe"), "PE", k)
            # The ladder shows ONE IV per strike, and it should be the OTM side's. An ITM
            # option is nearly all intrinsic, so its vol is inferred from a sliver of time
            # value and swings wildly on a stale print or a tick of rounding; the OTM side
            # of the same strike is all time value and is the number a trader means by
            # "the vol at 24000".
            iv = (pe["iv"] if k <= atm else ce["iv"])
            out.append({"strike": k,
                        "atm": k == atm,
                        "itm_ce": k <= atm,
                        "itm_pe": k >= atm,
                        "iv": iv if iv is not None else (ce["iv"] or pe["iv"]),
                        "ce": ce, "pe": pe})
        return out

    @staticmethod
    def _grid(rows: list[dict]) -> float:
        """The ladder's own strike step, so ``strike_window`` counts ROWS not points."""
        ks = sorted({float(r["strike"]) for r in rows})
        gaps = [b - a for a, b in zip(ks, ks[1:], strict=False) if b > a]
        return min(gaps) if gaps else 100.0

    # ----------------------------------------------------------------- state
    def state(self) -> dict:
        snap = (self.market.live_chain(self.underlying, self.expiry)
                if self.expiry else None) or {}
        # ONE spot, and it is the SELECTED expiry's own de-carried parity forward — the same
        # number live_chain anchors the ATM row to. The strip used to read index_spot (the
        # NEAREST expiry's), which quietly disagreed with the ladder: 24,620 in the header
        # against 24,599 in the chain on 2026-08-04, and at the expiry-day close the two
        # expiries diverged enough to print a basis of −110, which no 7-day future has.
        # With one source, "basis" is definitionally the carry we removed, so it is labelled
        # as carry rather than dressed up as a futures premium we cannot measure.
        fut = self.market._parity(self.expiry) if self.expiry else None
        spot = snap.get("spot") or self.market.index_spot(self.underlying)
        rows = self.chain_rows()
        quoted = sum(1 for r in rows for s in ("ce", "pe") if r[s]["quoted"])
        day_i = self.days.index(self.day)
        prev_close = None      # P2: the prior settled close for the change figures
        open_dt = datetime.combine(self.day, SESSION_OPEN)
        close_dt = datetime.combine(self.day, SESSION_CLOSE)
        played = (self.clock - open_dt).total_seconds() / max(
            1.0, (close_dt - open_dt).total_seconds())
        return {
            "session": {
                "id": self.id, "mode": "replay", "underlying": self.underlying,
                "lot_size": snap.get("lot_size") or 0,
                "date": self.day.isoformat(), "clock": self.clock.strftime("%H:%M"),
                "range": [SESSION_OPEN.strftime("%H:%M"), SESSION_CLOSE.strftime("%H:%M")],
                "played_pct": round(100 * max(0.0, min(1.0, played)), 2),
                "capital": self.capital, "status": "PAUSED",
                "has_prev_day": day_i > 0, "has_next_day": day_i < len(self.days) - 1,
            },
            "market": {
                "spot": spot, "fut": fut,
                "carry": (fut - spot) if (fut is not None and spot is not None) else None,
                "prev_close": prev_close,
                "day_open": self.market.spot_open, "day_high": self.market.spot_high,
                "day_low": self.market.spot_low,
                "expiry": self.expiry,
                "dte": ((date.fromisoformat(self.expiry) - self.day).days
                        if self.expiry else None),
            },
            "chain": {
                "expiry": self.expiry,
                "expiries": [{"iso": e, "dte": (date.fromisoformat(e) - self.day).days}
                             for e in self.tape.expiries
                             if date.fromisoformat(e) >= self.day],
                "atm_strike": snap.get("atm_strike"),
                "lot_size": snap.get("lot_size") or 0,
                "listing_grid": self.allow_fifty_strikes,
                "window": self.strike_window,
                "quoted": quoted, "total": 2 * len(rows),
                "rows": rows,
            },
            "legs": [], "staged": None,
            "risk": {"margin_source": "manual" if self.margin_per_lot_set else "model"},
            "alerts": [],
            "pricing": {"r": RISK_FREE, "q": 0.0, "t_floor_s": T_FLOOR_S,
                        "expiry_time": EXPIRY_TIME.strftime("%H:%M")},
            "notes": _notes(),
        }


def _notes() -> list[str]:
    """What the screen is not able to tell the truth about, said out loud rather than
    quietly faked. Rendered as footnotes — the design's numbers came from a live broker and
    a synthetic chain; ours come from a trades-only store."""
    return [
        "spot = put-call parity off the nearest expiry, de-carried at 6.5% — the store has "
        "no index series",
        "day open/high/low are the FORMING bar up to the cursor; the settled daily bar "
        "would be lookahead",
        "a strike with no print is shown as unquoted, never interpolated",
    ]
