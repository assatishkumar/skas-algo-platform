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

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from skas_algo.data.option_intraday_store import (
    captured_days,
    load_contract_bars,
    load_day,
)
from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.charges import charges_for_txn
from skas_algo.live.holidays import next_trading_day
from skas_algo.services.replay_market import ReplayChain, ReplayMarket

from . import presets as _presets
from .margin import MarginLeg, span_like

# The session window the store is filtered to. 15:40 is the post-CAS close the store itself
# measures (last minute-bar 15:39); before 2026-08-03 nothing trades past 15:29, so an
# over-wide end is harmless and an over-tight one silently truncates.
SESSION_OPEN = time(9, 15)
# Where a freshly opened day parks the cursor. NOT the open: the first minutes carry the
# widest spreads of the day and half the ladder has not printed yet, so 09:15 shows a chain
# nobody could have traded. 09:20 is where liquidity arrives (owner, 2026-09-09).
SESSION_DEFAULT = time(9, 20)
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

logger = logging.getLogger("skas_algo.console")


def _t_years(expiry_iso: str, now: datetime) -> float:
    exp = datetime.combine(date.fromisoformat(expiry_iso[:10]), EXPIRY_TIME)
    return max(T_FLOOR_S, (exp - now).total_seconds()) / _YEAR_S


@dataclass
class ConsoleLeg:
    """One leg of the console's own book.

    The batch replay keys its book by SYMBOL with {units, dir, entry}, which cannot express
    what this screen needs: two entries at the same strike bought minutes apart, a partial
    exit of 4 of 10 lots, or a leg switched off to see the payoff without it. So the console
    owns a leg list instead — ids are stable, and everything the UI does refers to one."""

    id: str
    symbol: str
    right: str
    strike: float
    expiry: str
    side: str                 # "B" | "S"
    lots: int
    lot_size: int
    entry: float
    entered_at: str
    enabled: bool = True
    realized: float = 0.0     # banked by partial exits of THIS leg
    exited_lots: int = 0

    @property
    def units(self) -> float:
        return float(self.lots * self.lot_size)

    @property
    def direction(self) -> int:
        return 1 if self.side == "B" else -1


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

        # REPLAY applies a click straight away; PAPER/LIVE will stage it for confirmation.
        # Rehearsing a structure means dozens of clicks and an Apply between each one is
        # friction with nothing to protect — the trade is imaginary. When the same screen
        # can reach a broker, that confirm step stops being friction and becomes the point,
        # so the machinery below stays and only this flag moves (owner, 2026-09-09).
        self.mode = "replay"
        self.legs: list[ConsoleLeg] = []
        self.staged: dict | None = None
        self._group = 0
        self.realized = 0.0
        self.charges = 0.0
        self._leg_seq = 0
        # The MASTER journal: every fill ever made, stamped with its minute, append-only and
        # never truncated by moving the cursor. The book is derived from the slice of it at
        # or before the clock, which is what makes rewinding past a trade unwind it AND
        # stepping forward again bring it back.
        self.journal: list[dict] = []
        self.fills: list[dict] = []      # the derived slice, ≤ cursor
        self._replaying = False
        # Armed levels. Each carries `fired_at` (a minute) once it trips; the cursor decides
        # what that means — before it the alert is armed, at or after it, fired — so a
        # rewind re-arms and stepping forward re-fires, like every other fact in a replay.
        self.alerts: list[dict] = []
        self._alert_seq = 0
        # Bookmarks are minutes ("2026-04-01T11:40") the owner flagged; the transport jumps
        # between them. The per-minute spot series backs "next 1% move" and is built once
        # per day, lazily, from a separate pass over the tape.
        self.bookmarks: list[str] = []
        self._margin_detail: dict | None = None
        self._spot_series: dict[str, list[tuple[str, float]]] = {}
        self.market = ReplayMarket(u, allow_fifty_strikes=self.allow_fifty_strikes)
        self.chain_view = ReplayChain(self.market)
        self.tape = _Tape(day=self.day)
        self.expiry: str | None = expiry
        self.clock: datetime = datetime.combine(self.day, SESSION_OPEN)
        self._open_day(self.day)
        self.seek(at or SESSION_DEFAULT.strftime("%H:%M"))

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

    def set_day(self, day: date, *, at: str | None = None) -> ConsoleSession:
        if day not in self.days:
            raise ValueError(f"{day.isoformat()} is not in the store for {self.underlying}")
        self._open_day(day)
        self.seek(at or SESSION_DEFAULT.strftime("%H:%M"))
        return self

    def shift_day(self, days: int) -> ConsoleSession:
        """±1d on the jog row: the NEXT captured day, not the next calendar day (a holiday
        or a capture gap would otherwise land on an empty session).

        The TIME OF DAY carries across. Comparing 09:30 on Tuesday with 09:30 on Wednesday
        is the whole reason to press +1d, and resetting to the open threw that away every
        time (owner, 2026-09-09)."""
        i = self.days.index(self.day)
        keep = self.clock.strftime("%H:%M")
        return self.set_day(self.days[max(0, min(len(self.days) - 1, i + days))], at=keep)

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
        self._settle_expired()
        self._replay_book(target)

    def _settle_expired(self) -> None:
        """A leg whose expiry has passed is SETTLED — a synthetic fill at the expiry day's
        15:30, at intrinsic off that day's closing parity spot, with no brokerage.

        It goes into the JOURNAL rather than being applied directly, so it obeys the same
        rule as every other fill: at a cursor before 15:30 on expiry day the leg is open;
        after it, it is settled; rewind and it is open again. Not undoable (`group` None) —
        an expiry is the market's action, not the owner's. Found because stepping from
        04 Aug to 05 Aug left a 0-DTE straddle alive, marked at the next series' prices."""
        opened = {}
        for f in self.journal:
            if f["action"] in ("BUY", "SHORT"):
                opened[f["symbol"]] = True
        settled = {f["symbol"] for f in self.journal if f["action"] == "SETTLE"}
        for symbol in opened:
            if symbol in settled:
                continue
            _u, exp_iso, strike_s, right = symbol.split("|")
            exp = date.fromisoformat(exp_iso)
            if exp > self.day:
                continue
            # On expiry day itself the SETTLE is stamped 15:30, and the journal replay does
            # the rest: before 15:30 the leg is open, after it is settled — the batch
            # replay's convention, so a console day and a replayed day end the same way.
            spot = self._close_spot_on(exp, exp_iso)
            strike = float(strike_s)
            if spot is None:
                # no expiry-day tape (a capture hole): settle at the contract's last print
                got = self.probe(right, strike) if exp_iso == self.expiry else None
                px = float(got["ltp"]) if got and got.get("found") else 0.0
            else:
                px = max(0.0, spot - strike) if right == "CE" else max(0.0, strike - spot)
            # net units still open for this symbol, from the journal itself
            net = 0.0
            for f in self.journal:
                if f["symbol"] != symbol:
                    continue
                if f["action"] in ("BUY", "SHORT"):
                    net += f["units"]
                else:
                    net -= f["units"]
            if net <= 0:
                continue
            c = charges_for_txn({"action": "SETTLE", "amount": net * px})
            self.journal.append({"at": f"{exp_iso}T15:30", "symbol": symbol,
                                 "action": "SETTLE", "group": None, "units": net,
                                 "price": round(px, 2), "charges": round(c["total"], 2),
                                 "note": "expired — settled to intrinsic"})
        self.journal.sort(key=lambda f: f["at"])

    def _close_spot_on(self, day: date, expiry_iso: str) -> float | None:
        """The parity spot of ``expiry_iso``'s own series at ``day``'s close. Loads that
        day's tape once and keeps the answer; None when the day was never captured."""
        cache = self.__dict__.setdefault("_settle_spots", {})
        key = f"{day.isoformat()}|{expiry_iso}"
        if key in cache:
            return cache[key]
        val = None
        if day in self.days:
            tape = self.tape if day == self.day else _Tape.load(self.underlying, day)
            if tape.symbols:
                m = ReplayMarket(self.underlying, allow_fifty_strikes=True)
                m.start_day(day, tape.all_symbols)
                for i in range(len(tape.symbols)):
                    m.quotes[tape.symbols[i]] = (tape.closes[i], tape.ois[i], tape.minutes[i])
                m._spot_dirty = True
                own = m._parity(expiry_iso)
                val = float(own) if own is not None else m.index_spot(self.underlying)
        cache[key] = val
        return val

    def _replay_book(self, target: datetime, *, force: bool = False) -> None:
        """Rebuild the book from the fill journal at the cursor.

        Rewinding past a trade UNWINDS it. That falls straight out of seeking being a
        replay-forward rather than a checkpoint restore, and it is the only answer that
        stays consistent: a fill at 09:21 cannot be in the book at 09:16 and then reappear
        at 09:22 unless the journal, not the object graph, is the source of truth."""
        key = target.strftime("%Y-%m-%dT%H:%M")
        if not self.journal:
            if force:                    # an undo that emptied the journal empties the book
                self.legs, self.realized, self.charges, self.fills = [], 0.0, 0.0, []
                self._leg_seq = 0
            return
        kept = [f for f in self.journal if f["at"] <= key]
        if not force and len(kept) == len(self.fills) and self.legs:
            return                       # already the right slice — nothing to rebuild
        self.legs, self.realized, self.charges, self.fills = [], 0.0, 0.0, []
        self._leg_seq = 0
        self._replaying = True
        try:
            for f in kept:
                self._reapply(f)
        finally:
            self._replaying = False

    def _reapply(self, fill: dict) -> None:
        _u, expiry, strike_s, right = fill["symbol"].split("|")
        strike, lot = float(strike_s), self._lot_size() or 1
        if fill["action"] in ("BUY", "SHORT"):
            # through _open, so a rebuilt book merges exactly as the live one did
            self._open(right, strike, "S" if fill["action"] == "SHORT" else "B",
                       max(1, int(fill["units"] // lot)), fill["price"], fill["at"],
                       expiry=expiry)
        else:
            for leg in self.legs:
                if leg.symbol == fill["symbol"]:
                    self._close(leg, int(fill["units"] // lot), fill["price"], fill["at"],
                                action=fill["action"] if fill["action"] == "SETTLE" else None)
                    break

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
        """Step the cursor. A step that runs PAST the session close rolls into the NEXT
        captured day (and past the open into the previous one), carrying the excess:
        15:40 +1m is the next session's 09:15, 15:40 +15m its 09:29. At the close the
        jogs used to pin at 15:40 with nothing happening (owner, 2026-09-09). The last
        captured day still pins — there is nowhere to go."""
        minutes = int(minutes)
        target = self.clock + timedelta(minutes=minutes)
        lo = datetime.combine(self.day, SESSION_OPEN)
        hi = datetime.combine(self.day, SESSION_CLOSE)
        i = self.days.index(self.day)
        if target > hi and minutes > 0:
            # A book whose every leg has EXPIRED is a finished cycle: the close is where the
            # replay stops, not a doorway into the next session (owner, 2026-09-09). A flat
            # book that never traded still rolls — there is nothing to have finished.
            if self.cycle_done():
                return self.seek(hi)
            if i + 1 < len(self.days):
                excess = int((target - hi).total_seconds() // 60)
                nxt = (datetime.combine(self.days[i + 1], SESSION_OPEN)
                       + timedelta(minutes=excess - 1))
                self.set_day(self.days[i + 1], at=SESSION_OPEN.strftime("%H:%M"))
                return self.seek(nxt)
            return self.seek(hi)
        if target < lo and minutes < 0:
            if i > 0:
                excess = int((lo - target).total_seconds() // 60)
                prev = (datetime.combine(self.days[i - 1], SESSION_CLOSE)
                        - timedelta(minutes=excess - 1))
                self.set_day(self.days[i - 1], at=SESSION_CLOSE.strftime("%H:%M"))
                return self.seek(prev)
            return self.seek(lo)
        return self.seek(target)

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

        held = self.held_by_strike()
        out = []
        for r in keep:
            k = r["strike"]
            ce, pe = leg(r.get("ce"), "CE", k), leg(r.get("pe"), "PE", k)
            ce["held"] = held.get(f"{self.expiry}|{int(k)}|CE")
            pe["held"] = held.get(f"{self.expiry}|{int(k)}|PE")
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

    # ----------------------------------------------------------------- the book
    def _price(self, right: str, strike: float, expiry: str | None = None) -> float | None:
        """What this contract is worth at the cursor. A leg can only be traded on a price
        the market actually printed — the probe's reference prices never reach this.

        ``expiry`` defaults to the SELECTED chip for a fresh click, but a held leg must
        always pass its own: without that, switching the chip re-priced every open leg off
        a different contract, and stepping past a leg's expiry marked a dead 04 Aug option
        at the 11 Aug option's price (found in a browser pass, 2026-09-09)."""
        sym = f"{self.underlying}|{expiry or self.expiry}|{int(strike)}|{right}"
        q = self.market.quotes.get(sym)
        return float(q[0]) if q else None

    def _lot_size(self) -> int:
        snap = self.market.live_chain(self.underlying, self.expiry) if self.expiry else None
        return int((snap or {}).get("lot_size") or 0)

    @property
    def requires_confirm(self) -> bool:
        """Only a book that can reach a broker needs an Apply between the click and the
        trade. In replay the trade is imaginary and the confirm is pure friction."""
        return self.mode != "replay"

    def stage(self, *, kind: str, right: str | None = None, strike: float | None = None,
              side: str | None = None, lots: int = 1, leg_id: str | None = None,
              enabled: bool | None = None, replace: bool = False) -> dict | None:
        """Apply a change, or stage it for confirmation — decided by the session's MODE.

        In REPLAY it happens immediately; ``undo_last`` is the safety net rather than a
        confirm step, and it is a better one because it also covers the click you regret a
        minute later. In PAPER/LIVE the change accumulates into a basket the way a structure
        is actually built (you cannot see a condor's payoff until the fourth leg lands), and
        Apply commits the lot. ``replace=True`` starts a fresh basket."""
        item = self._stage_item(kind=kind, right=right, strike=strike, side=side, lots=lots,
                                leg_id=leg_id, enabled=enabled)
        if not self.requires_confirm:
            self._group += 1
            self._apply(item, self.clock.strftime("%Y-%m-%dT%H:%M"))
            return None
        items = [] if (replace or not self.staged) else list(self.staged["items"])
        items.append(item)
        self.staged = {"items": items,
                       "label": " · ".join(i["label"] for i in items)}
        return self.staged

    def apply_basket(self, specs: list[dict], *, label: str | None = None) -> dict | None:
        """Several legs as ONE action — a preset, a hand-built basket. In replay they fill
        together under one `group` so Undo takes the whole structure back; in paper/live
        they replace the staged basket. Every leg must price or nothing is applied: a
        condor with three legs is a different trade, not a partial success."""
        items = [self._stage_item(kind="add", right=sp["right"], strike=sp["strike"],
                                  side=sp["side"], lots=int(sp.get("lots", 1)))
                 for sp in specs]
        if not items:
            raise ValueError("an empty basket")
        if not self.requires_confirm:
            self._group += 1
            minute = self.clock.strftime("%Y-%m-%dT%H:%M")
            for it in items:
                self._apply(it, minute)
            return None
        self.staged = {"items": items,
                       "label": label or " · ".join(i["label"] for i in items)}
        return self.staged

    def scale_book(self, factor: float) -> dict | None:
        """The design's multiplier: every leg's lots × ``factor`` in ONE action (one group,
        so Undo takes the whole rescale back). A rescale of 1 leg is a resize; of a condor
        it is four resizes that must all price — all-or-nothing like a basket. A factor
        that would take a leg below one lot is refused rather than rounded to zero."""
        if not self.legs:
            raise ValueError("nothing to scale")
        if factor <= 0:
            raise ValueError("the multiplier must be positive")
        items = []
        for leg in self.legs:
            want = int(round(leg.lots * factor))
            if want < 1:
                raise ValueError(f"{int(leg.strike)} {leg.right} would go below one lot")
            if want != leg.lots:
                items.append(self._stage_item(kind="resize", leg_id=leg.id, lots=want))
        if not items:
            return None
        if not self.requires_confirm:
            self._group += 1
            minute = self.clock.strftime("%Y-%m-%dT%H:%M")
            for it in items:
                self._apply(it, minute)
            return None
        self.staged = {"items": items, "label": f"Scale ×{factor:g}"}
        return self.staged

    def presets(self, lots: int = 1) -> list[dict]:
        """Every preset resolved against the chain at the cursor: concrete strikes, the
        margin the session would report for them, and a reason when one cannot be built.
        The card's max P/L, POP and breakevens are the frontend's `computeMetrics` over
        these legs — the same calculator as the rail, so a card cannot promise a number the
        rail will then contradict."""
        rows = self.chain_rows()
        snap = (self.market.live_chain(self.underlying, self.expiry)
                if self.expiry else None) or {}
        atm = snap.get("atm_strike")
        grid = self._grid(rows) if rows else 100.0
        lot = self._lot_size() or 1
        out = []
        for p in _presets.PRESETS:
            r = _presets.resolve(p, rows, atm, grid, lots)
            legs = []
            for x in r["legs"]:
                legs.append(ConsoleLeg(
                    id=f"P{x['i']}",
                    symbol=f"{self.underlying}|{self.expiry}|{int(x['strike'])}|{x['right']}",
                    right=x["right"], strike=x["strike"], expiry=self.expiry or "",
                    side=x["side"], lots=x["lots"], lot_size=lot, entry=x["ltp"],
                    entered_at=self.clock.strftime("%Y-%m-%dT%H:%M")))
            margin, source = self.margin(legs) if r["ok"] else (0.0, "model")
            credit = sum((1 if x["side"] == "S" else -1) * x["ltp"] * x["lots"] * lot
                         for x in r["legs"]) if r["ok"] else None
            out.append({"id": p.id, "name": p.name, "rule": p.rule, "defined": p.defined,
                        "tags": list(p.tags), "ok": r["ok"], "reason": r["reason"],
                        "legs": [self._leg_out(leg) for leg in legs] if r["ok"] else [],
                        "margin": margin, "margin_source": source,
                        "net_credit": round(credit, 2) if credit is not None else None})
        return out

    def apply_preset(self, preset_id: str, lots: int = 1) -> dict | None:
        p = _presets.BY_ID.get(preset_id)
        if not p:
            raise ValueError(f"unknown preset {preset_id!r}")
        rows = self.chain_rows()
        snap = (self.market.live_chain(self.underlying, self.expiry)
                if self.expiry else None) or {}
        grid = self._grid(rows) if rows else 100.0
        r = _presets.resolve(p, rows, snap.get("atm_strike"), grid, lots)
        if not r["ok"]:
            raise ValueError(f"{p.name}: {r['reason']}")
        return self.apply_basket(r["legs"], label=p.name)

    # ------------------------------------------------------------- replay track
    def _minute_key(self) -> str:
        return self.clock.strftime("%Y-%m-%dT%H:%M")

    def spot_series(self) -> list[tuple[str, float]]:
        """(minute, parity spot) for every minute of the open day — one pass over the tape
        with a scratch market, cached per day. Backs "next 1% move" and the track."""
        key = self.day.isoformat()
        if key in self._spot_series:
            return self._spot_series[key]
        m = ReplayMarket(self.underlying)
        m.start_day(self.day, self.tape.all_symbols)
        q = m.quotes
        mins, syms, closes, ois = (self.tape.minutes, self.tape.symbols,
                                   self.tape.closes, self.tape.ois)
        out: list[tuple[str, float]] = []
        prev = None
        for i in range(len(mins)):
            minute = mins[i]
            if minute != prev:
                if prev is not None:
                    m._spot_dirty = True
                    m.now = datetime.fromisoformat(prev)
                    sp = m.index_spot(self.underlying)
                    if sp:
                        out.append((prev, float(sp)))
                prev = minute
            q[syms[i]] = (closes[i], ois[i], minute)
        if prev is not None:
            m._spot_dirty = True
            m.now = datetime.fromisoformat(prev)
            sp = m.index_spot(self.underlying)
            if sp:
                out.append((prev, float(sp)))
        self._spot_series[key] = out
        return out

    def add_bookmark(self) -> list[str]:
        k = self._minute_key()
        if k not in self.bookmarks:
            self.bookmarks = sorted(self.bookmarks + [k])
        return self.bookmarks

    def remove_bookmark(self, minute: str) -> list[str]:
        self.bookmarks = [b for b in self.bookmarks if b != minute]
        return self.bookmarks

    def jump(self, kind: str, *, pct: float = 1.0) -> ConsoleSession:
        """Move the cursor to the next/previous EVENT: a fill, a bookmark, or a spot move of
        ``pct`` percent from where the cursor stands. Only events on the OPEN day — a jump
        that finds nothing leaves the cursor where it is (the route reports that)."""
        now = self._minute_key()
        day = self.day.isoformat()
        if kind in ("next_fill", "prev_fill"):
            mins = sorted({f["at"] for f in self.journal if f["at"].startswith(day)})
        elif kind in ("next_bookmark", "prev_bookmark"):
            mins = [b for b in self.bookmarks if b.startswith(day)]
        elif kind in ("next_move", "prev_move"):
            series = self.spot_series()
            here = next((sp for mk, sp in series if mk >= now), None) if series else None
            if here is None:
                return self
            fwd = kind == "next_move"
            cands = [mk for mk, sp in series
                     if (mk > now if fwd else mk < now) and abs(sp / here - 1) >= pct / 100.0]
            if not cands:
                return self
            target = min(cands) if fwd else max(cands)
            return self.seek(datetime.fromisoformat(target))
        else:
            raise ValueError(f"unknown jump {kind!r}")
        if kind.startswith("next"):
            later = [m for m in mins if m > now]
            return self.seek(datetime.fromisoformat(later[0])) if later else self
        earlier = [m for m in mins if m < now]
        return self.seek(datetime.fromisoformat(earlier[-1])) if earlier else self

    # ------------------------------------------------------------- the cycle
    def _traded_symbols(self) -> set[str]:
        return {f["symbol"] for f in self.journal if f["action"] in ("BUY", "SHORT")}

    def cycle_done(self) -> bool:
        """True when the book HAS traded and every leg it ever held has expired on or
        before the open day, with nothing open."""
        syms = self._traded_symbols()
        if not syms or self.legs:
            return False
        last = max(sym.split("|")[1] for sym in syms)
        return last <= self.day.isoformat()

    def cycle_info(self) -> dict | None:
        """The cycle's progress bar: from the first fill's day to the LAST expiry among the
        legs held, in captured sessions. None until something has traded."""
        syms = self._traded_symbols()
        if not syms:
            return None
        start = min(f["at"][:10] for f in self.journal if f["action"] in ("BUY", "SHORT"))
        held = {leg.expiry for leg in self.legs} or {sym.split("|")[1] for sym in syms}
        end = max(held)
        today = self.day.isoformat()
        # TRADING sessions, from the NSE calendar — not captured days. The store ends at
        # the last capture (today's bars land after 16:00), so a cycle whose expiry lies
        # beyond it read "12/13" with two weeks still to run (owner, 2026-09-09).
        span: list[str] = []
        d = date.fromisoformat(start)
        stop = date.fromisoformat(end)
        while d <= stop and len(span) < 400:
            span.append(d.isoformat())
            d = next_trading_day(d)
        total = max(1, len(span))
        open_dt = datetime.combine(self.day, SESSION_OPEN)
        close_dt = datetime.combine(self.day, SESSION_CLOSE)
        frac_today = (self.clock - open_dt).total_seconds() / max(
            1.0, (close_dt - open_dt).total_seconds())
        before = len([d for d in span if d < today])
        if self.cycle_done():
            pct = 100.0
        elif today <= end:
            # the expiry day itself progresses through its session like any other
            pct = 100.0 * (before + max(0.0, min(1.0, frac_today))) / total
        else:
            pct = 100.0
        last_captured = self.days[-1].isoformat() if self.days else today
        return {"start": start, "end": end, "sessions": total,
                "session_no": min(total, len([d for d in span if d <= today]) or 1),
                "pct": round(min(100.0, pct), 2), "done": self.cycle_done(),
                "legs_open": len(self.legs),
                # the expiry lies past the last captured session: the replay cannot reach it
                "beyond_data": end > last_captured, "data_until": last_captured}

    def save_payload(self) -> dict:
        return {"underlying": self.underlying, "day": self.day.isoformat(),
                "clock": self.clock.strftime("%H:%M"), "expiry": self.expiry,
                "capital": self.capital, "margin_per_lot_set": self.margin_per_lot_set,
                "allow_fifty_strikes": self.allow_fifty_strikes,
                "journal": self.journal, "alerts": self._alerts_out(),
                "bookmarks": self.bookmarks}

    def undo_last(self) -> bool:
        """Undo the last action — the whole action, so a roll's two fills and a basket's
        four legs go together. This is what replaces the confirm step: a misclick is
        cheaper to reverse than it is to prevent, and unlike a confirm it also covers the
        leg you decide against a minute later."""
        groups = [f.get("group") for f in self.journal if f.get("group")]   # SETTLE has None
        if not groups:
            return False
        last = max(groups)
        self.journal = [f for f in self.journal if f.get("group") != last]
        self._replay_book(self.clock, force=True)
        return True

    def _stage_item(self, *, kind: str, right: str | None = None, strike: float | None = None,
                    side: str | None = None, lots: int = 1, leg_id: str | None = None,
                    enabled: bool | None = None) -> dict:
        if kind == "add":
            if not (right and side and strike is not None):
                raise ValueError("an added leg needs a right, a side and a strike")
            px = self._price(right.upper(), strike)
            if px is None:
                raise ValueError(
                    f"{int(strike)} {right.upper()} has not traded at {self.clock:%H:%M} — "
                    "there is no price to fill against")
            lot = self._lot_size()
            return {"kind": "add", "right": right.upper(), "strike": float(strike),
                    "side": side.upper(), "lots": max(1, int(lots)),
                    "price": px, "lot_size": lot,
                    "label": f"{side.upper()} {int(strike)} {right.upper()} ×{lots}"}
        elif kind == "exit":
            leg = self._leg(leg_id)
            n = max(1, min(int(lots), leg.lots))
            px = self._price(leg.right, leg.strike, leg.expiry)
            if px is None:
                raise ValueError(f"{leg.symbol} has no price at {self.clock:%H:%M}")
            return {"kind": "exit", "leg_id": leg.id, "lots": n, "price": px,
                    "label": f"Exit {n} of {leg.lots} lots · {int(leg.strike)} {leg.right}"}
        elif kind == "toggle":
            leg = self._leg(leg_id)
            want = (not leg.enabled) if enabled is None else bool(enabled)
            verb = "Include" if want else "Exclude"
            return {"kind": "toggle", "leg_id": leg.id, "enabled": want,
                    "label": f"{verb} {int(leg.strike)} {leg.right}"}
        elif kind == "roll":
            # Move a leg to another strike: close it here, open the same size there. One
            # action, because "change the strike" is what the hand is doing.
            leg = self._leg(leg_id)
            if strike is None:
                raise ValueError("a roll needs a target strike")
            new_px = self._price(leg.right, float(strike), leg.expiry)
            old_px = self._price(leg.right, leg.strike, leg.expiry)
            if new_px is None or old_px is None:
                raise ValueError(f"{int(strike)} {leg.right} has no price at "
                                 f"{self.clock:%H:%M} to roll into")
            return {"kind": "roll", "leg_id": leg.id, "strike": float(strike),
                    "price": new_px, "exit_price": old_px, "lots": leg.lots,
                    "label": f"Roll {int(leg.strike)} → {int(strike)} {leg.right} ×{leg.lots}"}
        elif kind == "resize":
            leg = self._leg(leg_id)
            n = max(0, int(lots))
            if n == leg.lots:
                raise ValueError("that is the size it already is")
            px = self._price(leg.right, leg.strike, leg.expiry)
            if px is None:
                raise ValueError(f"{leg.symbol} has no price at {self.clock:%H:%M}")
            return {"kind": "resize", "leg_id": leg.id, "lots": n, "price": px,
                    "label": f"Resize {int(leg.strike)} {leg.right} ×{leg.lots} → ×{n}"}
        elif kind == "flatten":
            if not self.legs:
                raise ValueError("nothing to flatten")
            return {"kind": "flatten", "label": f"Close all {len(self.legs)} legs"}
        raise ValueError(f"unknown staged change {kind!r}")

    def discard(self) -> None:
        self.staged = None

    def commit(self) -> dict:
        """Apply every staged change at the cursor's price, with charges."""
        if not self.staged:
            raise ValueError("nothing staged")
        items = self.staged["items"]
        minute = self.clock.strftime("%Y-%m-%dT%H:%M")
        self._group += 1
        for st in items:
            self._apply(st, minute)
        self.staged = None
        return {"committed": len(items), "at": minute}

    def _apply(self, st: dict, minute: str) -> None:
        kind = st["kind"]
        if kind == "add":
            self._open(st["right"], st["strike"], st["side"], st["lots"], st["price"], minute)
        elif kind == "exit":
            self._close(self._leg(st["leg_id"]), st["lots"], st["price"], minute)
        elif kind == "toggle":
            self._leg(st["leg_id"]).enabled = st["enabled"]
        elif kind == "roll":
            leg = self._leg(st["leg_id"])
            side, lots, right = leg.side, leg.lots, leg.right
            expiry = leg.expiry
            self._close(leg, lots, st["exit_price"], minute)
            self._open(right, st["strike"], side, lots, st["price"], minute, expiry=expiry)
        elif kind == "resize":
            leg = self._leg(st["leg_id"])
            want = int(st["lots"])
            if want < leg.lots:
                self._close(leg, leg.lots - want, st["price"], minute)
            else:
                self._open(leg.right, leg.strike, leg.side, want - leg.lots,
                           st["price"], minute, expiry=leg.expiry)
        elif kind == "flatten":
            for leg in list(self.legs):
                px = self._price(leg.right, leg.strike, leg.expiry)
                if px is not None:
                    self._close(leg, leg.lots, px, minute)

    def _open(self, right: str, strike: float, side: str, lots: int, price: float,
              minute: str, *, expiry: str | None = None) -> None:
        """Buy or sell ``lots``, MERGING into the same contract on the same side.

        Adding to a position you already hold is one position at an average price — that is
        what a broker's book does, and what the positions table implies by showing a row per
        contract. Appending instead produced a second ×1 row every time the size stepper was
        pressed, so "×1" never changed and three clicks read as three legs at one strike
        (owner, 2026-09-09). The rare case this forecloses — holding two tranches of the same
        contract separately — is not what this screen is for, and the fills journal still has
        every entry if the history is ever wanted."""
        if lots <= 0:
            return
        exp = expiry or str(self.expiry)
        symbol = f"{self.underlying}|{exp}|{int(strike)}|{right}"
        existing = next((x for x in self.legs
                         if x.symbol == symbol and x.side == side and x.enabled), None)
        if existing is not None:
            added = lots * existing.lot_size
            total = existing.units + added
            existing.entry = (existing.entry * existing.units + price * added) / total
            existing.lots += lots
        else:
            self._leg_seq += 1
            existing = ConsoleLeg(
                id=f"L{self._leg_seq}", symbol=symbol, right=right, strike=float(strike),
                expiry=exp, side=side, lots=lots, lot_size=self._lot_size(),
                entry=price, entered_at=minute)
            self.legs.append(existing)
        self._charge("SHORT" if side == "S" else "BUY", lots * existing.lot_size, price,
                     minute, symbol)

    def _leg(self, leg_id: str | None) -> ConsoleLeg:
        for leg in self.legs:
            if leg.id == leg_id:
                return leg
        raise ValueError(f"no leg {leg_id!r}")

    def _close(self, leg: ConsoleLeg, lots: int, price: float, minute: str,
               action: str | None = None) -> None:
        n = max(0, min(int(lots), leg.lots))
        if not n:
            return
        units = n * leg.lot_size
        pnl = (price - leg.entry) * units * leg.direction
        # SETTLE pays no brokerage and no STT — the batch replay's convention, and the
        # exchange's: an expiry is not an order.
        pnl -= self._charge(action or ("COVER" if leg.side == "S" else "SELL"), units, price,
                            minute, leg.symbol)
        leg.realized += pnl
        leg.exited_lots += n
        self.realized += pnl
        leg.lots -= n
        if leg.lots <= 0:
            self.legs.remove(leg)

    def _charge(self, action: str, units: float, price: float, minute: str,
                symbol: str) -> float:
        c = charges_for_txn({"action": action, "amount": units * price})
        self.charges += c["total"]
        row = {"at": minute, "symbol": symbol, "action": action, "group": self._group,
               "units": units, "price": price, "charges": round(c["total"], 2)}
        self.fills.append(row)
        if not self._replaying:          # a replay re-derives the book; it does not re-trade
            self.journal.append(row)
        return c["total"]

    def restore(self, journal: list[dict], alerts: list[dict] | None = None,
                bookmarks: list[str] | None = None) -> None:
        """Rebuild a LOST session from the journal the page kept.

        The registry is in-process: a backend restart or an eviction drops the object, and
        before this every click after that was a 404 with the book gone. The page holds the
        full journal in its last state, so a new session on the same day can take it back
        and re-derive the book at the cursor exactly as a seek does. SETTLE rows are dropped
        and re-derived (the market's action, not the owner's); alerts come back armed and are
        re-evaluated at the cursor, so one that had fired fires again at the same minute."""
        rows = []
        for f in journal:
            if f.get("action") == "SETTLE":
                continue
            rows.append({"at": str(f["at"]), "symbol": str(f["symbol"]),
                         "action": str(f["action"]), "group": f.get("group"),
                         "units": float(f["units"]), "price": float(f["price"]),
                         "charges": float(f.get("charges") or 0.0)})
        rows.sort(key=lambda f: f["at"])
        self.journal = rows
        self._group = max([int(f["group"]) for f in rows if f.get("group")] or [0])
        self.alerts = []
        for a in alerts or []:
            try:
                self.arm_alert(str(a["kind"]), float(a["value"]), note=a.get("note"))
            except (KeyError, ValueError, TypeError):
                continue
        self.bookmarks = sorted({str(b) for b in (bookmarks or [])})
        self._settle_expired()
        self._replay_book(self.clock, force=True)

    def reset_book(self) -> None:
        """Start again: no legs, no journal, no realised, no charges.

        The console accumulates a SESSION's P&L, so after closing a structure the banked
        number stays on the rail — correct, and confusing when you then build something new
        and its "realised" is money the previous position made. This is the way back to a
        clean slate without reopening the day."""
        self.legs, self.journal, self.fills = [], [], []
        self.realized = self.charges = 0.0
        self._leg_seq = 0
        self.staged = None
        for a in self.alerts:
            a["fired_at"], a["fired_value"] = None, None

    # ----------------------------------------------------------------- risk
    def _leg_out(self, leg: ConsoleLeg) -> dict:
        ltp = self._price(leg.right, leg.strike, leg.expiry)
        pnl = ((ltp - leg.entry) * leg.units * leg.direction) if ltp is not None else None
        out = {"id": leg.id, "symbol": leg.symbol, "right": leg.right, "strike": leg.strike,
               "expiry": leg.expiry, "side": leg.side, "lots": leg.lots,
               "lot_size": leg.lot_size, "units": leg.units, "direction": leg.direction,
               "entry": round(leg.entry, 2), "ltp": ltp,
               "pnl": round(pnl, 2) if pnl is not None else None,
               "enabled": leg.enabled, "realized": round(leg.realized, 2)}
        out["dte"] = (date.fromisoformat(leg.expiry) - self.day).days if leg.expiry else None
        out.update(self._leg_greeks(leg, out))
        return out

    def margin(self, legs: list[ConsoleLeg] | None = None) -> tuple[float, str]:
        """Margin, and — just as important — WHERE THE NUMBER CAME FROM.

        A manual anchor (the real broker figure for one lot-set, the `margin_per_set`
        precedent) is the only accurate answer here. The fallback is the platform's model,
        which is span+exposure on the SHORTS and blind to long hedges: on the design's own
        bear call spread it reads ₹19.4L against a Kite basket's ₹3.64L. That is 5.3x, and in
        the direction that makes a hedged structure look unaffordable — so every percentage
        measured against it is labelled with its source rather than presented as fact."""
        book = [leg for leg in (self.legs if legs is None else legs) if leg.enabled]
        if self.margin_per_lot_set:
            sets = max((leg.lots for leg in book if leg.side == "S"), default=0)
            self._margin_detail = None
            return round(self.margin_per_lot_set * sets, 2), "manual"
        # SPAN-shaped: the book's worst scenario loss (hedges offset) + 2% exposure on
        # every short unit (nothing offsets). See margin.py for the calibration. The old
        # per-short-leg span+exposure sum read ₹4.1L for a 1-lot straddle.
        spot = self.market.index_spot(self.underlying) or 0.0
        mlegs = []
        for leg in book:
            px = self._price(leg.right, leg.strike, leg.expiry) or leg.entry
            t = _t_years(leg.expiry, self.clock)
            iv = bs.implied_vol(px, spot, leg.strike, t, RISK_FREE, leg.right) if spot else None
            mlegs.append(MarginLeg(leg.right, leg.strike, leg.direction, leg.units,
                                   iv or 0.0, t))
        d = span_like(mlegs, spot, r=RISK_FREE)
        self._margin_detail = d
        return d["total"], "model"

    def _staged_out(self) -> dict | None:
        """The staged change, plus the book it WOULD produce. The frontend draws the dotted
        curve and the before→after risk from `after_legs` using the same payoff maths it uses
        for the live book, so the preview and the commit cannot disagree."""
        if not self.staged:
            return None
        after = self._project()
        margin_after, _src = self.margin(after)
        margin_now, src = self.margin()
        return {**self.staged,
                "after_legs": [self._leg_out(leg) for leg in after if leg.enabled],
                "margin_before": margin_now, "margin_after": margin_after,
                "margin_source": src}

    def held_by_strike(self) -> dict:
        """Net lots per strike+right, so the LADDER can show where the position sits.

        Reading a chain with a position on it and no marks means holding the strikes in your
        head — the design puts an S×10 / B×10 badge on the row for exactly that reason."""
        out: dict[str, dict] = {}
        for leg in self.legs:
            key = f"{leg.expiry}|{int(leg.strike)}|{leg.right}"
            row = out.setdefault(key, {"lots": 0, "side": leg.side, "enabled": False})
            row["lots"] += leg.lots * leg.direction
            row["enabled"] = row["enabled"] or leg.enabled
        return {k: {"lots": abs(v["lots"]), "side": "B" if v["lots"] > 0 else "S",
                    "enabled": v["enabled"]}
                for k, v in out.items() if v["lots"]}

    def _project(self) -> list[ConsoleLeg]:
        """The book as the staged change would leave it — a COPY; nothing here is applied."""
        import copy

        book = [copy.copy(leg) for leg in self.legs]
        now = self.clock.strftime("%Y-%m-%dT%H:%M")
        for i, st in enumerate((self.staged or {}).get("items", [])):
            kind = st["kind"]
            if kind in ("add", "roll"):
                if kind == "roll":
                    src = next((x for x in book if x.id == st["leg_id"]), None)
                    if src is None:
                        continue
                    side, lots, right = src.side, src.lots, src.right
                    book = [x for x in book if x.id != st["leg_id"]]
                else:
                    side, lots, right = st["side"], st["lots"], st["right"]
                book.append(ConsoleLeg(
                    id=f"STAGED{i}",
                    symbol=f"{self.underlying}|{self.expiry}|{int(st['strike'])}|{right}",
                    right=right, strike=st["strike"], expiry=str(self.expiry), side=side,
                    lots=lots, lot_size=st.get("lot_size") or self._lot_size(),
                    entry=st["price"], entered_at=now))
            elif kind == "exit":
                for leg in book:
                    if leg.id == st["leg_id"]:
                        leg.lots = max(0, leg.lots - int(st["lots"]))
                book = [leg for leg in book if leg.lots > 0]
            elif kind == "resize":
                for leg in book:
                    if leg.id == st["leg_id"]:
                        leg.lots = int(st["lots"])
                book = [leg for leg in book if leg.lots > 0]
            elif kind == "toggle":
                for leg in book:
                    if leg.id == st["leg_id"]:
                        leg.enabled = bool(st["enabled"])
            elif kind == "flatten":
                book = []
        return book

    def _leg_greeks(self, leg: ConsoleLeg, out: dict) -> dict:
        """Per-SHARE, position-signed greeks, the exact convention of
        engine/live.py::_enrich_greeks (a short leg reads Θ positive, Γ and Vega negative,
        Θ per calendar day, Vega per 1% of IV) so a console leg and a live leg read alike.
        Solved off the leg's OWN contract at the cursor."""
        ltp = out.get("ltp")
        spot = self.market.index_spot(self.underlying)
        if ltp is None or not spot:
            return {"iv": None, "delta": None, "gamma": None, "theta": None, "vega": None}
        t = _t_years(leg.expiry, self.clock)
        iv = bs.implied_vol(float(ltp), spot, leg.strike, t, RISK_FREE, leg.right)
        if iv is None:
            return {"iv": None, "delta": None, "gamma": None, "theta": None, "vega": None}
        dr = leg.direction
        return {
            "iv": round(iv * 100, 2),
            "delta": round(dr * bs.delta(spot, leg.strike, t, RISK_FREE, iv, leg.right), 4),
            "gamma": round(dr * bs.gamma(spot, leg.strike, t, RISK_FREE, iv), 6),
            "theta": round(dr * bs.theta(spot, leg.strike, t, RISK_FREE, iv, leg.right) / 365.0, 2),
            "vega": round(dr * bs.vega(spot, leg.strike, t, RISK_FREE, iv) / 100.0, 2),
        }

    def _net_greeks(self, legs_out: list[dict]) -> dict:
        """Position totals: per-share greek × units, summed over ENABLED legs — the design's
        "× LOT SIZE ON" reading, which is the one that answers "how much delta am I
        actually carrying". None when no leg could be solved."""
        tot = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        have = False
        for x in legs_out:
            if not x["enabled"] or x.get("delta") is None:
                continue
            have = True
            for k in tot:
                tot[k] += float(x[k]) * float(x["units"])
        if not have:
            return {"delta": None, "gamma": None, "theta": None, "vega": None}
        return {"delta": round(tot["delta"], 2), "gamma": round(tot["gamma"], 4),
                "theta": round(tot["theta"], 0), "vega": round(tot["vega"], 0)}

    # ----------------------------------------------------------------- alerts
    def arm_alert(self, kind: str, value: float, *, note: str | None = None) -> dict:
        """Arm a level. ``target``/``stop`` are rupees of TOTAL MTM (stop is a loss, given as
        a positive number); ``delta`` is |net Δ| in units; ``above``/``below`` are spot."""
        if kind not in ("target", "stop", "delta", "above", "below"):
            raise ValueError(f"unknown alert kind {kind!r}")
        self._alert_seq += 1
        a = {"id": f"A{self._alert_seq}", "kind": kind, "value": float(value),
             "note": note, "fired_at": None, "fired_value": None}
        self.alerts.append(a)
        return a

    def clear_alert(self, alert_id: str) -> bool:
        before = len(self.alerts)
        self.alerts = [a for a in self.alerts if a["id"] != alert_id]
        return len(self.alerts) < before

    def _evaluate_alerts(self, mtm: float, net_delta: float | None, spot: float | None) -> None:
        """Trip armed alerts against the book AT THE CURSOR, once each. A fired alert
        holds its minute; if the cursor is ever before that minute it is armed again,
        because in a replay what has not happened yet has not happened."""
        now = self.clock.strftime("%Y-%m-%dT%H:%M")
        for a in self.alerts:
            if a["fired_at"] and a["fired_at"] > now:
                a["fired_at"], a["fired_value"] = None, None     # rewound past it
            if a["fired_at"]:
                continue
            k, v = a["kind"], a["value"]
            hit = ((k == "target" and mtm >= v)
                   or (k == "stop" and mtm <= -abs(v))
                   or (k == "delta" and net_delta is not None and abs(net_delta) >= v)
                   or (k == "above" and spot is not None and spot >= v)
                   or (k == "below" and spot is not None and spot <= v))
            if hit:
                a["fired_at"] = now
                a["fired_value"] = round(mtm if k in ("target", "stop")
                                         else (net_delta if k == "delta" else spot) or 0.0, 2)

    def _alerts_out(self) -> list[dict]:
        return [{**a, "state": "fired" if a["fired_at"] else "armed"} for a in self.alerts]

    def _risk_out(self) -> dict:
        margin, source = self.margin()
        detail = self._margin_detail
        open_pnl = sum(x["pnl"] or 0.0 for x in (self._leg_out(leg) for leg in self.legs
                                                 if leg.enabled))
        return {
            "realised": round(self.realized, 2),
            "unrealised": round(open_pnl, 2),
            "mtm": round(self.realized + open_pnl, 2),
            "charges": round(self.charges, 2),
            "margin": margin,
            # NEVER just a number: the model is an estimate (SPAN-shaped, calibrated to one
            # Kite basket), so a "% of margin" against it is only as honest as this label.
            "margin_source": source,
            "margin_detail": detail,          # {span, exposure, total, worst_move_pct} | None
            "capital": self.capital,
            "legs_open": len([leg for leg in self.legs if leg.enabled]),
        }

    # ----------------------------------------------------------------- probe
    def probe(self, right: str, strike: float, *, look_back_days: int = 10) -> dict:
        """The most recent print for one contract AT OR BEFORE the cursor, hunting back
        through earlier sessions when today has none.

        The ladder leaves an untraded strike blank on purpose — a price nobody paid is not
        a quote. But "blank" and "worthless" look identical, and for a wing you are weighing
        up, yesterday's close is real information. So this is on demand and comes back
        LABELLED with its age: the caller shows it as a reference, never as a live mark, and
        neither the book nor any risk figure ever reads it."""
        right = right.upper()
        if right not in ("CE", "PE") or not self.expiry:
            raise ValueError("probe needs a CE/PE and a selected expiry")
        sym = f"{self.underlying}|{self.expiry}|{int(strike)}|{right}"
        q = self.market.quotes.get(sym)
        if q is not None:                       # already on today's tape
            seen = datetime.fromisoformat(q[2])
            return {"symbol": sym, "ltp": float(q[0]), "at": q[2],
                    "age_min": int((self.clock - seen).total_seconds() // 60),
                    "days_back": 0, "found": True}
        # Walk back over CAPTURED days only — a calendar walk spends its budget on weekends
        # and holidays and gives up before reaching a day that traded.
        i = self.days.index(self.day)
        window = self.days[max(0, i - look_back_days): i + 1]
        try:
            bars = load_contract_bars(self.underlying, self.expiry, strike, right,
                                      window[0], self.day)
        except Exception:  # pragma: no cover - a probe must never break the screen
            logger.exception("probe failed for %s", sym)
            return {"symbol": sym, "found": False}
        if bars is None or bars.empty:
            return {"symbol": sym, "found": False}
        upto = bars[pd.to_datetime(bars["start"]) <= self.clock]
        if upto.empty:
            return {"symbol": sym, "found": False}
        row = upto.iloc[-1]
        at = pd.to_datetime(row["start"]).to_pydatetime()
        return {"symbol": sym, "ltp": float(row["close"]),
                "at": at.isoformat(timespec="minutes"),
                "age_min": int((self.clock - at).total_seconds() // 60),
                "days_back": (self.day - at.date()).days, "found": True}

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
        legs_out = [self._leg_out(leg) for leg in self.legs]
        greeks = self._net_greeks(legs_out)
        risk = self._risk_out()
        self._evaluate_alerts(risk["mtm"], greeks["delta"], spot)
        day_i = self.days.index(self.day)
        prev_close = None      # P2: the prior settled close for the change figures
        open_dt = datetime.combine(self.day, SESSION_OPEN)
        close_dt = datetime.combine(self.day, SESSION_CLOSE)
        played = (self.clock - open_dt).total_seconds() / max(
            1.0, (close_dt - open_dt).total_seconds())
        return {
            "session": {
                "id": self.id, "mode": self.mode, "underlying": self.underlying,
                "lot_size": snap.get("lot_size") or 0,
                "date": self.day.isoformat(), "clock": self.clock.strftime("%H:%M"),
                "range": [SESSION_OPEN.strftime("%H:%M"), SESSION_CLOSE.strftime("%H:%M")],
                "played_pct": round(100 * max(0.0, min(1.0, played)), 2),
                "capital": self.capital, "status": "PAUSED",
                "requires_confirm": self.requires_confirm,
                "can_undo": bool([f for f in self.journal if f.get("group")]),
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
            "legs": legs_out,
            "staged": self._staged_out(),
            "risk": {**risk, "greeks": greeks},
            "fills": self.fills[-40:],
            "journal": self.journal,          # the whole tape of actions — what a restore needs
            "alerts": self._alerts_out(),
            "bookmarks": self.bookmarks,
            "cycle": self.cycle_info(),
            # the replay track: markers on the OPEN day for the scrubber
            "track": {
                "fills": [{"at": f["at"][11:], "action": f["action"]}
                          for f in self.journal if f["at"].startswith(self.day.isoformat())],
                "alerts": [{"at": a["fired_at"][11:], "kind": a["kind"]}
                           for a in self.alerts
                           if a["fired_at"] and a["fired_at"].startswith(self.day.isoformat())],
                "bookmarks": [b[11:] for b in self.bookmarks
                              if b.startswith(self.day.isoformat())],
            },
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
