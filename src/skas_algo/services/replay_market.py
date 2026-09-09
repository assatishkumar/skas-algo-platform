"""The replay MARKET: a minute-clock façade over the 1-min option store.

These four classes moved here VERBATIM from ``services/intraday_replay.py`` (2026-09-09) so
a second consumer — the interactive Options Console — can drive the same market the batch
replay drives, rather than forking it. A fork is exactly how two answers to "what was this
leg worth at 09:30" come to disagree, and the batch replay is the reference the console has
to be honest against.

Nothing about the behaviour changed in the move, and nothing should change here without
running ``tests/test_intraday_replay.py`` — including its sha256 report pin, which exists to
catch drift in this file forever rather than only at extraction time.

The underscore names are the ORIGINAL ones, kept so the move is a pure relocation and the
replay driver reads unchanged; ``intraday_replay`` re-imports them as they are. Everything
that is not the batch replay should use the public aliases at the bottom.
"""

from __future__ import annotations

from datetime import date, datetime

from skas_algo.engine.options.contract_specs import lot_size_for, strike_allowed

class _Market:
    """ctx.market for the replay: per-day forward-filled marks + store-built chains."""

    def __init__(self, underlying: str, lot_overrides: dict | None = None,
                 allow_fifty_strikes: bool = False, stale_minutes: int = 5):
        self.underlying = underlying
        self.lot_overrides = lot_overrides   # params["contract_specs"] — parity w/ engine
        # BACKTEST-ONLY escape hatch: lift the NIFTY 100-multiples rule so a replay can
        # mirror pre-2026-07-14 live history (which legitimately traded 50s) or probe
        # 50-strike variants. A harness param — strategies and LIVE paths never see it.
        self.allow_fifty_strikes = allow_fifty_strikes
        # A print older than this many minutes is NOT a print (``has_print`` → False, so a
        # strategy defers its decision exactly as it does live on a stale mark). The store
        # is trades only: a deep-ITM leg can go HOURS between prints while the index moves
        # 1,000 points, and "printed today" then hands the strategy a price a week old.
        # 0 = the pre-2026-09-07 rule (printed today). Harness param ``mark_stale_min``.
        self.stale_minutes = max(0, int(stale_minutes))
        self.quotes: dict[str, tuple[float, float, str]] = {}   # symbol -> (close, oi, minute)
        self._floor_cache: dict[str, float | None] = {}          # expiry -> parity F (per minute)
        # The last close of every symbol across days — the daily-close mark of an open leg
        # that did not print today (the live view's forward-filled ``_last_close``). Never
        # read by a decision: ``close``/``has_print`` stay strictly today's prints.
        self.last_marks: dict[str, float] = {}
        # expiry_iso -> strike -> {"CE": sym, "PE": sym}; rebuilt per day from stored symbols.
        self.chains: dict[str, dict[float, dict[str, str]]] = {}
        self.current_date: date | None = None
        self.now: datetime | None = None
        # index_spot memo: quotes are stable between mutations, but index_spot is called
        # several times per minute (sizing, note_spot, margin push, strategy internals),
        # each re-scanning every strike via _parity. Cache it; any quote write dirties it.
        self._spot_dirty = True
        self._spot_cache: float | None = None

    def start_day(self, day: date, symbols: list[str]) -> None:
        self.current_date = day
        self.quotes = {}          # live marks don't survive overnight — neither do these
        self._spot_dirty = True
        self.chains = {}
        # Today's FORMING index bar (running O/H/L off the parity spot) — feeds ema21's
        # bands with chart-at-decision-time semantics instead of the settled bar.
        self.spot_open: float | None = None
        self.spot_high: float | None = None
        self.spot_low: float | None = None
        for sym in symbols:
            _u, e, strike_s, right = sym.split("|")
            self.chains.setdefault(e, {}).setdefault(float(strike_s), {})[right] = sym

    def note_spot(self) -> float | None:
        """Sample the current parity spot into today's forming bar (called per minute
        only when a strategy consumes daily bars — the parity scan isn't free)."""
        s = self.index_spot(self.underlying)
        if s is None:
            return None
        if self.spot_open is None:
            self.spot_open = s
        self.spot_high = s if self.spot_high is None else max(self.spot_high, s)
        self.spot_low = s if self.spot_low is None else min(self.spot_low, s)
        return s

    def feed(self, symbol: str, close: float, oi: float) -> None:
        stamp = self.now.strftime("%Y-%m-%dT%H:%M") if self.now else ""
        self.quotes[symbol] = (float(close), float(oi), stamp)
        self._spot_dirty = True

    def close(self, symbol: str) -> float:
        """The leg's last print, floored at its DISCOUNTED INTRINSIC. An option never
        trades below intrinsic, but a stale print of a deep-ITM leg sits wherever the
        index was when it last traded: on 2025-10-17 a BANKNIFTY condor's four legs were
        marked at prices that valued it at ₹390/unit when it was intrinsically worth 0 —
        a max-loss month booked as a target win, ₹1.2L of fiction on 300 units — and the
        same stale long wing marked the book ₹5.9L underwater mid-cycle. The floor uses
        the same expiry's parity forward (the liquid ATM pair) and the harness's own
        carry discount, so it is exact on expiry day and a few points conservative before."""
        q = self.quotes.get(symbol)
        if q is None:
            raise KeyError(symbol)
        floor = self._intrinsic_floor(symbol)
        return q[0] if floor is None or q[0] >= floor else floor

    def has_print(self, symbol: str) -> bool:
        q = self.quotes.get(symbol)
        if q is None:
            return False
        if not self.stale_minutes or self.now is None or not q[2]:
            return True
        try:
            age = self.now - datetime.fromisoformat(q[2])
        except (TypeError, ValueError):  # pragma: no cover - a malformed stamp is not stale
            return True
        return age.total_seconds() <= self.stale_minutes * 60

    def _intrinsic_floor(self, symbol: str) -> float | None:
        try:
            _u, e, strike_s, right = symbol.split("|")
            k = float(strike_s)
        except ValueError:
            return None
        if self._spot_dirty:
            self._floor_cache = {}
        if e not in self._floor_cache:
            self._floor_cache[e] = self._parity(e)
        f = self._floor_cache[e]
        # A thin far expiry can hand back a parity pair that is stale on one side; if its
        # forward sits more than 3% from the liquid nearest-expiry spot, use that spot
        # (undiscounted intrinsic — a slightly looser, always-valid bound).
        spot = self.index_spot(self.underlying)
        if f is None or f <= 0 or (spot and abs(f - spot) > 0.03 * spot):
            if not spot:
                return None
            return max(0.0, spot - k if right == "CE" else k - spot)
        intr = max(0.0, f - k if right == "CE" else k - f)
        return intr * (self._decarry(f, e) / f)

    def mark_or_floor(self, symbol: str) -> float | None:
        """The daily-close mark for an open leg: today's (floored) print, else YESTERDAY'S
        mark, floored. Two holes taught the fallback. 2022-07-22: a BANKNIFTY fly's lower
        wing sat 3,000 points ITM and did not trade all day; carried at ENTRY beside a
        live-marked body it put a ₹7L hole in the equity curve that vanished the next
        session. 2025-01-02: the whole 2025-01-29 chain went dark in the store for the rest
        of the month, and flooring the ITM wing at intrinsic while the ATM body (floor 0)
        stayed at entry mixed two bases into a ₹3.4L loss that was not there. A carried
        mark keeps every leg on the SAME basis, one day stale; the floor still catches a
        carry the index has since run through. None = nothing known: leave it at entry."""
        try:
            return self.close(symbol)
        except KeyError:
            pass
        carried = self.last_marks.get(symbol)
        floor = self._intrinsic_floor(symbol)
        if carried is None:
            return floor or None
        return max(carried, floor) if floor is not None else carried

    def end_day(self) -> None:
        """Roll today's closes into the cross-day marks (after the daily-close marking)."""
        for sym, q in self.quotes.items():
            self.last_marks[sym] = q[0]

    def _parity(self, expiry_iso: str) -> float | None:
        best = None
        for k, legs in self.chains.get(expiry_iso, {}).items():
            ce = self.quotes.get(legs.get("CE", ""))
            pe = self.quotes.get(legs.get("PE", ""))
            if ce is None or pe is None:
                continue
            diff = abs(ce[0] - pe[0])
            if best is None or diff < best[0]:
                best = (diff, k + ce[0] - pe[0])
        return best[1] if best else None

    # Put-call parity yields the FUTURES-implied level (cash + cost-of-carry). The
    # strategies compare "spot" against CASH-index strikes, and the ~20-pt carry bias
    # flipped the 2026-07-16 ATM pick to 24200 while live (real index spot) picked 24100
    # — a 100-pt strike miss that decided the day. Discount at the same flat r the
    # strategies price with; dividends ignored (residual bias is a few points, not ~20).
    _CARRY_R = 0.065

    def _decarry(self, f: float, expiry_iso: str) -> float:
        try:
            t_days = (date.fromisoformat(str(expiry_iso)[:10]) - self.current_date).days
        except (TypeError, ValueError):  # pragma: no cover - malformed expiry → leave as-is
            return f
        if t_days <= 0:
            return f  # expiry day: F ≈ S (also keeps settlement intrinsic exact)
        return f / (1.0 + self._CARRY_R * t_days / 365.0)

    def index_spot(self, _u: str) -> float | None:
        if not self._spot_dirty:
            return self._spot_cache
        val = None
        for e in sorted(self.chains):   # nearest stored expiry that has a parity pair
            spot = self._parity(e)
            if spot is not None:
                val = self._decarry(spot, e)
                break
        self._spot_cache = val
        self._floor_cache = {}
        self._spot_dirty = False
        return val

    def live_chain(self, _u: str, expiry_iso: str) -> dict | None:
        strikes = self.chains.get(str(expiry_iso)[:10])
        if not strikes:
            return None
        rows = []
        for k in sorted(strikes):
            if not self.allow_fifty_strikes and not strike_allowed(self.underlying, k):
                continue  # same NIFTY-100 coarsening the LIVE chain applies

            def info(sym: str | None) -> dict | None:
                q = self.quotes.get(sym) if sym else None
                return None if q is None else {"ltp": q[0], "oi": int(q[1])}

            legs = strikes[k]
            rows.append({"strike": k, "ce": info(legs.get("CE")), "pe": info(legs.get("PE"))})
        own = self._parity(str(expiry_iso)[:10])
        spot = (self._decarry(own, expiry_iso) if own is not None
                else self.index_spot(self.underlying))
        if not rows or spot is None:
            return None
        atm = min((r["strike"] for r in rows), key=lambda s: abs(s - spot))
        try:
            lot = lot_size_for(self.underlying, date.fromisoformat(str(expiry_iso)[:10]),
                               overrides=self.lot_overrides)
        except KeyError:
            lot = 0
        return {"spot": spot, "atm_strike": atm, "lot_size": lot, "rows": rows}


class _ChainRow:
    """OptionChainView-row lookalike built from the store's forward-filled marks. The
    ratio family + ema21 read exactly {strike,right,close,oi,symbol}; ``symbol`` is the
    STORE symbol ("U|iso|strike|right") — LOAD-BEARING: it flows straight into Signal
    and the replay's _fill splits it on "|" (the engine's instrument symbols would not
    round-trip)."""

    __slots__ = ("underlying", "expiry", "strike", "right", "close", "settle", "oi", "symbol")

    def __init__(self, underlying, expiry, strike, right, close, oi, symbol):
        self.underlying = underlying
        self.expiry = expiry
        self.strike = strike
        self.right = right
        self.close = close
        self.settle = None
        self.oi = oi
        self.symbol = symbol


class _Chain:
    """ctx.option_chain() — the cached-chain interface (OptionChainView subset) the
    positional strategies consume, served from the store's minute marks. Only contracts
    that have PRINTED so far today appear (the store is sparse — untraded far wings are
    simply absent, and the strategies' own oi>0/_bad guards skip them, exactly as they
    skip a phantom bhavcopy strike)."""

    def __init__(self, market: "_Market | None" = None):
        self.market = market
        self.days: list[date] = []

    def expiries(self, _u: str, _today: date) -> list[date]:
        return list(self.days)

    def spot(self, u: str, _today: date) -> float | None:
        return self.market.index_spot(u) if self.market is not None else None

    def expiry_for_dte(self, _u: str, today: date, dte_target: int) -> date | None:
        """Expiry nearest ``dte_target`` days out (ties → sooner) — mirrors
        engine/options/chain.py so hni picks the same weekly here as in live."""
        future = [e for e in self.days if e >= today]
        if not future:
            return None
        return min(future, key=lambda e: (abs((e - today).days - dte_target), (e - today).days))

    def chain(self, u: str, _today: date, expiry: date) -> list["_ChainRow"]:
        if self.market is None:
            return []
        e_iso = expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry)[:10]
        rows: list[_ChainRow] = []
        for strike, legs in sorted((self.market.chains.get(e_iso) or {}).items()):
            if not self.market.allow_fifty_strikes and not strike_allowed(u, strike):
                continue   # same NIFTY-100 coarsening as live_chain and the EOD view
            for right, sym in legs.items():
                q = self.market.quotes.get(sym)
                if q is None:
                    continue   # never printed today — absent, like an untraded strike
                rows.append(_ChainRow(u, expiry, strike, right, q[0], int(q[1]), sym))
        return rows


class _Ctx:
    def __init__(self, market: _Market, chain: _Chain):
        self.market = market
        self.chain = chain
        self.positions: dict[str, dict] = {}   # symbol -> {units, dir, entry}
        self._now: datetime | None = None

    def now(self) -> datetime:
        return self._now

    def today(self) -> date:
        return self._now.date()

    def option_chain(self):
        return self.chain

    def lots(self, symbol: str):
        return 1 if symbol in self.positions else 0

    def close(self, symbol: str) -> float:
        return self.market.close(symbol)


# Public names for consumers outside the batch replay (the Options Console). The underscore
# names above are a relocation artefact, not an invitation to reach past these.
ReplayMarket = _Market
ReplayChainRow = _ChainRow
ReplayChain = _Chain
ReplayCtx = _Ctx
