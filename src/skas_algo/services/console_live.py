"""The Options Console over a RUNNING deployment — paper or live.

Same screen, same `ConsoleState` DTO, a different source: the chain is the deployment's
live chain, the book is its portfolio, and a commit is `LiveRun.manual_order` — the ONE
already-gated way a hand reaches an order (§1). Nothing here places, modifies or cancels
anything itself; it stages a basket and hands it to the run, which fills it on its paper
broker or, if and only if every §1 key is set, through `LiveBroker`.

Deliberately OUTSIDE `services/options_console/` (which is pinned never to import from
`live/`): this module is the bridge and the only place the console touches the manager.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta

from skas_algo.engine.options import black_scholes as bs
from skas_algo.engine.options.instrument import make as make_option
from skas_algo.engine.options.instrument import parse
from skas_algo.live.manager import manager
from skas_algo.services.options_console import presets as _presets
from skas_algo.services.options_console.alerts import AlertBook
from skas_algo.services.options_console.margin import MarginLeg, span_like
from skas_algo.services.options_console.session import (
    EXPIRY_TIME,
    RISK_FREE,
    SESSION_CLOSE,
    SESSION_OPEN,
    T_FLOOR_S,
    _t_years,
)

logger = logging.getLogger("skas_algo.console")

_LOCK = threading.Lock()
_CONSOLES: dict[int, LiveConsole] = {}


def _ist_now() -> datetime:
    from skas_algo.live.quotes import IST

    return datetime.now(IST).replace(tzinfo=None)


class LiveConsole(AlertBook):
    """A console view + staging basket over one `LiveRun`."""

    def __init__(self, live, *, expiry: str | None = None, strike_window: int = 20) -> None:
        if str(live.config.instrument_class).upper() != "DERIV" or not live.config.underlying:
            raise ValueError("the console drives an OPTIONS deployment (DERIV with an underlying)")
        self.live = live
        self.id = f"live:{live.run_id}"
        self.underlying = str(live.config.underlying).upper()
        self.strike_window = max(4, int(strike_window))
        self.staged: dict | None = None
        self.disabled: set[str] = set()  # legs excluded from the payoff (view only)
        self._init_alerts()
        self.expiry = expiry or self._default_expiry()

    # ---------------------------------------------------------------- sources
    @property
    def session(self):
        return self.live.session

    @property
    def market(self):
        return self.live.session.market

    @property
    def mode(self) -> str:
        """``live`` only when the run is LIVE-mode AND its orders reach a broker right now;
        a demoted LIVE run (paper broker after a restart) is ``paper`` here too."""
        if str(self.live.config.mode).upper() != "LIVE":
            return "paper"
        snap = self._snap()
        return "live" if snap.get("order_broker") == "live" else "paper"

    @property
    def requires_confirm(self) -> bool:
        return True

    def _snap(self) -> dict:
        try:
            return self.live.snapshot() or {}
        except Exception:  # pragma: no cover - a snapshot blip must not blank the console
            logger.exception("console: snapshot failed for run %s", self.live.run_id)
            return {}

    def _today(self) -> date:
        return _ist_now().date()

    def _clock(self) -> datetime:
        return _ist_now().replace(second=0, microsecond=0)

    def expiries(self) -> list[str]:
        today = self._today()
        out: set[str] = set()
        chain = getattr(self.market, "chain", None)
        if chain is not None and hasattr(chain, "expiries"):
            try:
                out |= {e.isoformat() for e in chain.expiries(self.underlying, today) if e >= today}
            except Exception:  # pragma: no cover - thin chain
                pass
        for sym in self.session.portfolio.lot_symbols():
            inst = parse(sym)
            if inst is not None:
                out.add(inst.expiry.isoformat())
        e = getattr(self.session.strategy, "entry_expiry", None)
        if e:
            out.add(e.isoformat() if hasattr(e, "isoformat") else str(e)[:10])
        return sorted(out)

    def _default_expiry(self) -> str | None:
        """The chip a run opens on: the NEAREST expiry it actually HOLDS (a calendar's near
        leg), else the strategy's own entry expiry, else the nearest listed. Opening on the
        strategy's expiry put a ⚠ on every row of a book held on a later series."""
        held = sorted(
            inst.expiry.isoformat()
            for inst in (parse(sym) for sym in self.session.portfolio.lot_symbols())
            if inst is not None
        )
        if held:
            return held[0]
        e = getattr(self.session.strategy, "entry_expiry", None)
        if e:
            return e.isoformat() if hasattr(e, "isoformat") else str(e)[:10]
        exps = self.expiries()
        return exps[0] if exps else None

    def set_expiry(self, expiry: str) -> None:
        self.expiry = expiry

    def _lot_size(self, expiry: str | None = None) -> int:
        try:
            inst = make_option(
                self.underlying,
                date.fromisoformat((expiry or self.expiry)[:10]),
                100.0,
                "CE",
                lot_overrides=getattr(self.session.strategy, "lot_overrides", None),
            )
            return int(inst.lot_size)
        except Exception:
            return 0

    # ---------------------------------------------------------------- the chain
    def _live_chain(self) -> dict | None:
        if not self.expiry:
            return None
        try:
            return self.market.live_chain(self.underlying, self.expiry)
        except Exception:  # pragma: no cover
            return None

    def spot(self) -> float | None:
        snap = self._live_chain() or {}
        sp = snap.get("spot")
        if sp:
            return float(sp)
        try:
            sp = self.market.index_spot(self.underlying)
        except Exception:
            sp = None
        return float(sp) if sp else None

    @staticmethod
    def _grid(strikes: list[float]) -> float:
        ks = sorted(set(strikes))
        gaps = [b - a for a, b in zip(ks, ks[1:], strict=False) if b > a]
        return min(gaps) if gaps else 100.0

    def chain_rows(self) -> list[dict]:
        snap = self._live_chain()
        if not snap or not self.expiry:
            return []
        spot = float(snap.get("spot") or self.spot() or 0.0)
        raw = snap.get("rows") or []
        strikes = [float(r["strike"]) for r in raw if r.get("strike") is not None]
        if not strikes or not spot:
            return []
        atm = float(snap.get("atm_strike") or min(strikes, key=lambda k: abs(k - spot)))
        grid = self._grid(strikes)
        t = _t_years(self.expiry, self._clock())
        held = self.held_by_strike()

        def leg(cell: dict | None, right: str, strike: float) -> dict:
            ltp = float(cell.get("ltp") or 0) if cell else 0.0
            if not ltp:
                return {
                    "ltp": None,
                    "oi": None,
                    "iv": None,
                    "delta": None,
                    "quoted": False,
                    "stale_min": None,
                    "bid": None,
                    "ask": None,
                }
            iv = bs.implied_vol(ltp, spot, strike, t, RISK_FREE, right)
            d = bs.delta(spot, strike, t, RISK_FREE, iv, right) if iv else None
            return {
                "ltp": ltp,
                "oi": cell.get("oi"),
                "iv": round(iv * 100, 2) if iv else None,
                "delta": round(d, 4) if d is not None else None,
                "quoted": True,
                "stale_min": 0,
                "bid": cell.get("bid"),
                "ask": cell.get("ask"),
            }

        out = []
        for r in raw:
            k = float(r["strike"])
            if abs(k - atm) > self.strike_window * grid:
                continue
            ce, pe = leg(r.get("ce"), "CE", k), leg(r.get("pe"), "PE", k)
            ce["held"] = held.get(f"{self.expiry}|{int(k)}|CE")
            pe["held"] = held.get(f"{self.expiry}|{int(k)}|PE")
            iv = pe["iv"] if k <= atm else ce["iv"]
            out.append(
                {
                    "strike": k,
                    "atm": k == atm,
                    "itm_ce": k <= atm,
                    "itm_pe": k >= atm,
                    "iv": iv if iv is not None else (ce["iv"] or pe["iv"]),
                    "ce": ce,
                    "pe": pe,
                }
            )
        return out

    def _price(self, right: str, strike: float, expiry: str | None = None) -> float | None:
        """The mark the run itself would fill against: its market view's close."""
        exp = expiry or self.expiry
        if not exp:
            return None
        try:
            inst = make_option(
                self.underlying,
                date.fromisoformat(exp[:10]),
                float(strike),
                right.upper(),
                lot_overrides=getattr(self.session.strategy, "lot_overrides", None),
            )
            px = self.market.close(inst.symbol)
        except Exception:
            return None
        return float(px) if px else None

    # ---------------------------------------------------------------- the book
    def legs(self) -> list[dict]:
        closes = self.market.mark_prices()
        out = []
        for symbol in self.session.portfolio.lot_symbols():
            inst = parse(symbol, getattr(self.session.strategy, "lot_overrides", None))
            if inst is None:
                continue
            lots = self.session.portfolio.lots(symbol)
            units = sum(lot.units for lot in lots)
            if not units:
                continue
            cost = sum(lot.units * lot.price for lot in lots)
            direction = lots[0].direction
            entry = cost / units
            ltp = closes.get(symbol)
            lot_size = int(inst.lot_size or 1)
            pnl = direction * ((ltp or entry) - entry) * units if ltp is not None else None
            exp = inst.expiry.isoformat()
            out.append(
                {
                    "id": symbol,
                    "symbol": symbol,
                    "right": inst.right,
                    "strike": float(inst.strike),
                    "expiry": exp,
                    "side": "S" if direction < 0 else "B",
                    "lots": max(1, units // lot_size),
                    "lot_size": lot_size,
                    "units": units,
                    "direction": direction,
                    "entry": round(entry, 2),
                    "ltp": round(float(ltp), 2) if ltp is not None else None,
                    "pnl": round(pnl, 2) if pnl is not None else None,
                    "enabled": symbol not in self.disabled,
                    "realized": 0.0,
                    "dte": (inst.expiry - self._today()).days,
                }
            )
        return out

    def held_by_strike(self) -> dict:
        return {
            f"{leg['expiry']}|{int(leg['strike'])}|{leg['right']}": {
                "side": leg["side"],
                "lots": leg["lots"],
            }
            for leg in self.legs()
        }

    def _greeks(self, leg: dict, spot: float | None) -> dict:
        none = {"iv": None, "delta": None, "gamma": None, "theta": None, "vega": None}
        if leg.get("ltp") is None or not spot:
            return none
        t = _t_years(leg["expiry"], self._clock())
        iv = bs.implied_vol(float(leg["ltp"]), spot, leg["strike"], t, RISK_FREE, leg["right"])
        if iv is None:
            return none
        dr = leg["direction"]
        return {
            "iv": round(iv * 100, 2),
            "delta": round(dr * bs.delta(spot, leg["strike"], t, RISK_FREE, iv, leg["right"]), 4),
            "gamma": round(dr * bs.gamma(spot, leg["strike"], t, RISK_FREE, iv), 6),
            "theta": round(
                dr * bs.theta(spot, leg["strike"], t, RISK_FREE, iv, leg["right"]) / 365.0, 2
            ),
            "vega": round(dr * bs.vega(spot, leg["strike"], t, RISK_FREE, iv) / 100.0, 2),
        }

    def margin(
        self, legs: list[dict], spot: float | None, snap: dict
    ) -> tuple[float, str, dict | None]:
        """The RUN's own margin when it has one (a Zerodha basket, a manual anchor), else
        the SPAN-shaped estimate over the staged/held legs."""
        used = snap.get("margin_used")
        src = snap.get("margin_source")
        if used and src and src != "model" and legs is not None and not self.staged:
            return float(used), str(src), None
        if not spot:
            return 0.0, "model", None
        ml = []
        for leg in legs:
            px = leg.get("ltp") or leg.get("entry") or 0.0
            t = _t_years(leg["expiry"], self._clock())
            iv = (
                bs.implied_vol(float(px), spot, leg["strike"], t, RISK_FREE, leg["right"])
                if px
                else None
            )
            ml.append(
                MarginLeg(leg["right"], leg["strike"], leg["direction"], leg["units"], iv or 0.0, t)
            )
        d = span_like(ml, spot, r=RISK_FREE)
        return d["total"], "model", d

    # ---------------------------------------------------------------- staging
    def _item(
        self,
        *,
        kind: str,
        right=None,
        strike=None,
        side=None,
        lots: int = 1,
        leg_id=None,
        enabled=None,
    ) -> dict:
        legs = {leg["id"]: leg for leg in self.legs()}
        if kind == "add":
            if not (right and side and strike is not None):
                raise ValueError("an added leg needs a right, a side and a strike")
            px = self._price(right, float(strike))
            if px is None:
                raise ValueError(f"{int(strike)} {right.upper()} has no live price")
            return {
                "kind": "add",
                "right": right.upper(),
                "strike": float(strike),
                "side": side.upper(),
                "lots": max(1, int(lots)),
                "price": px,
                "expiry": self.expiry,
                "lot_size": self._lot_size(),
                "label": f"{side.upper()} {int(strike)} {right.upper()} ×{lots}",
            }
        if kind == "flatten":
            if not legs:
                raise ValueError("nothing to flatten")
            return {"kind": "flatten", "label": f"Close all {len(legs)} legs"}
        leg = legs.get(leg_id or "")
        if leg is None:
            raise ValueError(f"no such leg {leg_id!r}")
        if kind == "exit":
            n = max(1, min(int(lots), leg["lots"]))
            return {
                "kind": "exit",
                "leg_id": leg["id"],
                "lots": n,
                "price": leg["ltp"],
                "label": f"Exit {n} of {leg['lots']} lots · {int(leg['strike'])} {leg['right']}",
            }
        if kind == "toggle":
            want = (not leg["enabled"]) if enabled is None else bool(enabled)
            return {
                "kind": "toggle",
                "leg_id": leg["id"],
                "enabled": want,
                "label": f"{'Include' if want else 'Exclude'} {int(leg['strike'])} {leg['right']}",
            }
        if kind == "roll":
            if strike is None:
                raise ValueError("a roll needs the strike to roll to")
            px = self._price(leg["right"], float(strike), leg["expiry"])
            if px is None:
                raise ValueError(f"{int(strike)} {leg['right']} has no live price")
            return {
                "kind": "roll",
                "leg_id": leg["id"],
                "strike": float(strike),
                "price": px,
                "label": f"Roll {int(leg['strike'])} → {int(strike)} {leg['right']}",
            }
        if kind == "resize":
            n = max(0, int(lots))
            if n == leg["lots"]:
                raise ValueError("that is the size it already is")
            return {
                "kind": "resize",
                "leg_id": leg["id"],
                "lots": n,
                "price": leg["ltp"],
                "label": f"Resize {int(leg['strike'])} {leg['right']} ×{leg['lots']} → ×{n}",
            }
        raise ValueError(f"unknown staged change {kind!r}")

    def _edit_staged(self, *, kind: str, leg_id: str, strike=None, lots: int = 1) -> bool:
        """A change to a leg that is itself still STAGED edits the staged add in place —
        a resize sets its lots, a roll moves its strike, an exit trims it (to zero =
        drops it). Stacking a second item on an uncommitted leg would have sent the run
        an exit for a leg it does not hold."""
        if not self.staged or not str(leg_id).startswith("S"):
            return False
        adds = [it for it in self.staged["items"] if it["kind"] == "add"]
        try:
            idx = int(str(leg_id)[1:]) - 1
        except ValueError:
            return False
        if idx < 0 or idx >= len(adds):
            return False
        it = adds[idx]
        if kind == "resize":
            n = max(0, int(lots))
            if n == 0:
                self.staged["items"].remove(it)
            else:
                it["lots"] = n
        elif kind == "exit":
            n = max(0, it["lots"] - max(1, int(lots)))
            if n == 0:
                self.staged["items"].remove(it)
            else:
                it["lots"] = n
        elif kind == "roll":
            px = self._price(it["right"], float(strike), it.get("expiry"))
            if px is None:
                raise ValueError(f"{int(strike)} {it['right']} has no live price")
            it["strike"], it["price"] = float(strike), px
        else:
            return False
        it["label"] = f"{it['side']} {int(it['strike'])} {it['right']} ×{it['lots']}"
        items = self.staged["items"]
        self.staged = (
            {"items": items, "label": " · ".join(i["label"] for i in items)} if items else None
        )
        return True

    def stage(
        self,
        *,
        kind: str,
        right=None,
        strike=None,
        side=None,
        lots: int = 1,
        leg_id=None,
        enabled=None,
        replace: bool = False,
    ) -> dict | None:
        if kind in ("exit", "resize", "roll") and self._edit_staged(
            kind=kind, leg_id=leg_id or "", strike=strike, lots=lots
        ):
            return self.staged
        item = self._item(
            kind=kind,
            right=right,
            strike=strike,
            side=side,
            lots=lots,
            leg_id=leg_id,
            enabled=enabled,
        )
        if kind == "toggle":  # a VIEW change, never an order
            if item["enabled"]:
                self.disabled.discard(item["leg_id"])
            else:
                self.disabled.add(item["leg_id"])
            return None
        items = [] if (replace or not self.staged) else list(self.staged["items"])
        items.append(item)
        self.staged = {"items": items, "label": " · ".join(i["label"] for i in items)}
        return self.staged

    def apply_basket(self, specs: list[dict], *, label: str | None = None) -> dict | None:
        items = [
            self._item(
                kind="add",
                right=sp["right"],
                strike=sp["strike"],
                side=sp["side"],
                lots=int(sp.get("lots", 1)),
            )
            for sp in specs
        ]
        if not items:
            raise ValueError("an empty basket")
        self.staged = {"items": items, "label": label or " · ".join(i["label"] for i in items)}
        return self.staged

    def presets(self, lots: int = 1) -> list[dict]:
        rows = self.chain_rows()
        snap = self._live_chain() or {}
        atm = snap.get("atm_strike")
        if atm is None and rows:
            atm = next((r["strike"] for r in rows if r["atm"]), None)
        grid = self._grid([r["strike"] for r in rows]) if rows else 100.0
        lot = self._lot_size() or 1
        spot = self.spot()
        out = []
        for p in _presets.PRESETS:
            r = _presets.resolve(p, rows, atm, grid, lots)
            legs = (
                [
                    {
                        "id": f"P{x['i']}",
                        "symbol": (
                            f"{self.underlying}|{self.expiry}|" f"{int(x['strike'])}|{x['right']}"
                        ),
                        "right": x["right"],
                        "strike": x["strike"],
                        "expiry": self.expiry or "",
                        "side": x["side"],
                        "lots": x["lots"],
                        "lot_size": lot,
                        "units": x["lots"] * lot,
                        "direction": -1 if x["side"] == "S" else 1,
                        "entry": x["ltp"],
                        "ltp": x["ltp"],
                        "pnl": 0.0,
                        "enabled": True,
                        "realized": 0.0,
                        "dte": None,
                    }
                    for x in r["legs"]
                ]
                if r["ok"]
                else []
            )
            for leg in legs:
                leg.update(self._greeks(leg, spot))
            m, src, _ = self.margin(legs, spot, {}) if r["ok"] else (0.0, "model", None)
            credit = (
                sum((1 if x["side"] == "S" else -1) * x["ltp"] * x["lots"] * lot for x in r["legs"])
                if r["ok"]
                else None
            )
            out.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "rule": p.rule,
                    "defined": p.defined,
                    "tags": list(p.tags),
                    "ok": r["ok"],
                    "reason": r["reason"],
                    "legs": legs,
                    "margin": m,
                    "margin_source": src,
                    "net_credit": round(credit, 2) if credit is not None else None,
                }
            )
        return out

    def apply_preset(self, preset_id: str, lots: int = 1) -> dict | None:
        p = _presets.BY_ID.get(preset_id)
        if not p:
            raise ValueError(f"unknown preset {preset_id!r}")
        rows = self.chain_rows()
        snap = self._live_chain() or {}
        atm = snap.get("atm_strike") or next((r["strike"] for r in rows if r["atm"]), None)
        r = _presets.resolve(
            p, rows, atm, self._grid([x["strike"] for x in rows]) if rows else 100.0, lots
        )
        if not r["ok"]:
            raise ValueError(f"{p.name}: {r['reason']}")
        return self.apply_basket(r["legs"], label=p.name)

    def scale_book(self, factor: float) -> dict | None:
        legs = self.legs()
        if not legs:
            raise ValueError("nothing to scale")
        if factor <= 0:
            raise ValueError("the multiplier must be positive")
        items = []
        for leg in legs:
            want = int(round(leg["lots"] * factor))
            if want < 1:
                raise ValueError(f"{int(leg['strike'])} {leg['right']} would go below one lot")
            if want != leg["lots"]:
                items.append(self._item(kind="resize", leg_id=leg["id"], lots=want))
        if not items:
            return None
        self.staged = {"items": items, "label": f"Scale ×{factor:g}"}
        return self.staged

    def discard(self) -> None:
        self.staged = None

    def _orders(self, items: list[dict]) -> tuple[list[dict], list[dict]]:
        """Staged items → the manual-order request: FIFO-unit closes and expiry-bearing
        opens. This is the whole bridge; nothing else about an order is decided here."""
        legs = {leg["id"]: leg for leg in self.legs()}
        closes: list[dict] = []
        opens: list[dict] = []
        for it in items:
            k = it["kind"]
            if k == "add":
                opens.append(
                    {
                        "right": it["right"],
                        "strike": it["strike"],
                        "lots": it["lots"],
                        "side": "sell" if it["side"] == "S" else "buy",
                        "expiry": it.get("expiry") or self.expiry,
                    }
                )
            elif k == "exit":
                leg = legs[it["leg_id"]]
                if it["lots"] >= leg["lots"]:
                    closes.append({"symbol": leg["symbol"]})
                else:
                    closes.append({"symbol": leg["symbol"], "units": it["lots"] * leg["lot_size"]})
            elif k == "roll":
                leg = legs[it["leg_id"]]
                closes.append({"symbol": leg["symbol"]})
                opens.append(
                    {
                        "right": leg["right"],
                        "strike": it["strike"],
                        "lots": leg["lots"],
                        "side": "sell" if leg["side"] == "S" else "buy",
                        "expiry": leg["expiry"],
                    }
                )
            elif k == "resize":
                leg = legs[it["leg_id"]]
                want = int(it["lots"])
                if want == 0:
                    closes.append({"symbol": leg["symbol"]})
                elif want < leg["lots"]:
                    closes.append(
                        {"symbol": leg["symbol"], "units": (leg["lots"] - want) * leg["lot_size"]}
                    )
                else:
                    opens.append(
                        {
                            "right": leg["right"],
                            "strike": leg["strike"],
                            "lots": want - leg["lots"],
                            "side": "sell" if leg["side"] == "S" else "buy",
                            "expiry": leg["expiry"],
                        }
                    )
            elif k == "flatten":
                closes.extend({"symbol": leg["symbol"]} for leg in legs.values())
        return closes, opens

    def ticket(self) -> dict | None:
        """Design D5: the orders the basket becomes, one row each, at the price the run
        would fill against NOW (its mark; paper fills at the touch, live through the
        LIMIT-at-touch ladder), with the cash each moves and the net. What Commit sends,
        in the words a broker's ticket uses — so nothing is sent that was not read."""
        if not self.staged:
            return None
        legs = {leg["id"]: leg for leg in self.legs()}
        closes, opens = self._orders(self.staged["items"])
        rows: list[dict] = []
        lot = self._lot_size() or 1
        for c in closes:
            leg = next((x for x in legs.values() if x["symbol"] == c["symbol"]), None)
            if leg is None:
                continue
            units = int(c.get("units") or leg["units"])
            px = leg.get("ltp") or leg["entry"]
            closing_short = leg["side"] == "S"
            rows.append(
                {
                    "action": "BUY" if closing_short else "SELL",
                    "role": "close",
                    "symbol": leg["symbol"],
                    "right": leg["right"],
                    "strike": leg["strike"],
                    "expiry": leg["expiry"],
                    "lots": max(1, units // (leg["lot_size"] or 1)),
                    "units": units,
                    "price": px,
                    "order_type": "MKT",
                    "cash": round((-1 if closing_short else 1) * px * units, 2),
                }
            )
        for o in opens:
            px = self._price(o["right"], float(o["strike"]), o.get("expiry")) or 0.0
            units = int(o["lots"]) * lot
            selling = o["side"] == "sell"
            rows.append(
                {
                    "action": "SELL" if selling else "BUY",
                    "role": "open",
                    "symbol": (
                        f"{self.underlying}|{o.get('expiry') or self.expiry}|"
                        f"{int(o['strike'])}|{o['right']}"
                    ),
                    "right": o["right"],
                    "strike": float(o["strike"]),
                    "expiry": o.get("expiry") or self.expiry,
                    "lots": int(o["lots"]),
                    "units": units,
                    "price": px,
                    "order_type": "MKT",
                    "cash": round((1 if selling else -1) * px * units, 2),
                }
            )
        return {
            "rows": rows,
            "net_cash": round(sum(r["cash"] for r in rows), 2),
            "fill_basis": (
                "the run's LIMIT-at-touch ladder"
                if self.mode == "live"
                else "the run's paper broker, at the touch"
            ),
            "limit_orders": False,
        }

    def commit(self) -> dict:
        """Hand the basket to the run. Paper fills on its PaperBroker; a LIVE run with every
        §1 key set fills through LiveBroker — the gate is the run's, not ours."""
        if not self.staged:
            raise ValueError("nothing staged")
        closes, opens = self._orders(self.staged["items"])
        if not closes and not opens:
            self.staged = None
            return {"committed": 0, "events": []}
        events = self.live.manual_order(closes=closes, opens=opens)
        n = len(self.staged["items"])
        self.staged = None
        logger.info(
            "console: run %s applied %s staged item(s) → %s event(s) [%s]",
            self.live.run_id,
            n,
            len(events or []),
            self.mode,
        )
        return {"committed": n, "events": events}

    # ---------------------------------------------------------------- the DTO
    def _project(self, legs: list[dict]) -> list[dict]:
        """The book AS IF the staged basket were applied — what the page shows on a
        running deployment until Commit. Touched rows carry ``pending``."""
        if not self.staged:
            return legs
        book = [dict(leg) for leg in legs]
        seq = 0
        for st in self.staged["items"]:
            k = st["kind"]
            if k == "add":
                sym = f"{self.underlying}|{st['expiry']}|{int(st['strike'])}|{st['right']}"
                same = next(
                    (
                        b
                        for b in book
                        if b["symbol"] == sym
                        and b["side"] == st["side"]
                        and not str(b["id"]).startswith("S")
                    ),
                    None,
                )
                lot = st["lot_size"] or 1
                seq += 1
                if same:
                    same["lots"] += st["lots"]
                    same["units"] = same["lots"] * lot
                    same["pending"] = "add"
                else:
                    book.append(
                        {
                            "id": f"S{seq}",
                            "symbol": sym,
                            "right": st["right"],
                            "strike": st["strike"],
                            "expiry": st["expiry"],
                            "side": st["side"],
                            "lots": st["lots"],
                            "lot_size": lot,
                            "units": st["lots"] * lot,
                            "direction": -1 if st["side"] == "S" else 1,
                            "entry": st["price"],
                            "ltp": st["price"],
                            "pnl": 0.0,
                            "enabled": True,
                            "realized": 0.0,
                            "dte": (
                                (date.fromisoformat(st["expiry"]) - self._today()).days
                                if st.get("expiry")
                                else None
                            ),
                            "pending": "add",
                        }
                    )
            elif k == "exit":
                for b in book:
                    if b["id"] == st["leg_id"]:
                        b["lots"] -= st["lots"]
                        b["units"] = b["lots"] * b["lot_size"]
                        b["pending"] = "exit"
                book = [b for b in book if b["lots"] > 0]
            elif k == "roll":
                for b in book:
                    if b["id"] == st["leg_id"]:
                        b["strike"] = st["strike"]
                        b["entry"] = st["price"]
                        b["ltp"] = st["price"]
                        b["symbol"] = (
                            f"{self.underlying}|{b['expiry']}|" f"{int(b['strike'])}|{b['right']}"
                        )
                        b["pnl"] = 0.0
                        b["pending"] = "roll"
            elif k == "resize":
                for b in book:
                    if b["id"] == st["leg_id"]:
                        b["lots"] = int(st["lots"])
                        b["units"] = b["lots"] * b["lot_size"]
                        b["pending"] = "resize"
                book = [b for b in book if b["lots"] > 0]
            elif k == "flatten":
                book = []
        spot = self.spot()
        for b in book:
            if b.get("pending"):
                b.update(self._greeks(b, spot))
        return book

    def state(self) -> dict:
        snap = self._snap()
        now = self._clock()
        today = self._today()
        spot = self.spot()
        rows = self.chain_rows()
        quoted = sum(1 for r in rows for s in ("ce", "pe") if r[s]["quoted"])
        legs = self.legs()
        for leg in legs:
            leg.update(self._greeks(leg, spot))
        enabled = [leg for leg in legs if leg["enabled"]]
        tot = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
        have = False
        for x in enabled:
            if x.get("delta") is None:
                continue
            have = True
            for k in tot:
                tot[k] += float(x[k]) * float(x["units"])
        greeks = (
            {
                "delta": round(tot["delta"], 2),
                "gamma": round(tot["gamma"], 4),
                "theta": round(tot["theta"], 0),
                "vega": round(tot["vega"], 0),
            }
            if have
            else {"delta": None, "gamma": None, "theta": None, "vega": None}
        )
        realised = float(snap.get("realized_pnl") or 0.0)
        open_pnl = sum(x["pnl"] or 0.0 for x in enabled)
        margin, src, detail = self.margin(enabled, spot, snap)
        mtm = realised + open_pnl
        self._evaluate_alerts(
            now.strftime("%Y-%m-%dT%H:%M"), mtm, greeks["delta"], spot, rewindable=False
        )
        after = self._project(legs)
        risk_after = None
        if self.staged:
            en_after = [b for b in after if b["enabled"]]
            m_after, src_after, d_after = self.margin(en_after, spot, {})
            tot_a = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
            have_a = False
            for x in en_after:
                if x.get("delta") is None:
                    continue
                have_a = True
                for k in tot_a:
                    tot_a[k] += float(x[k]) * float(x["units"])
            open_after = sum(x.get("pnl") or 0.0 for x in en_after)
            risk_after = {
                "realised": round(realised, 2),
                "unrealised": round(open_after, 2),
                "mtm": round(realised + open_after, 2),
                "charges": 0.0,
                "margin": m_after,
                "margin_source": src_after,
                "margin_detail": d_after,
                "capital": float(self.live.config.capital),
                "legs_open": len(en_after),
                "greeks": (
                    {
                        "delta": round(tot_a["delta"], 2),
                        "gamma": round(tot_a["gamma"], 4),
                        "theta": round(tot_a["theta"], 0),
                        "vega": round(tot_a["vega"], 0),
                    }
                    if have_a
                    else {"delta": None, "gamma": None, "theta": None, "vega": None}
                ),
            }
        else:
            m_after, src_after = margin, src
        exps = self.expiries()
        open_dt = datetime.combine(today, SESSION_OPEN)
        close_dt = datetime.combine(today, SESSION_CLOSE)
        played = (now - open_dt).total_seconds() / max(1.0, (close_dt - open_dt).total_seconds())
        mode = self.mode
        fills = [
            {
                "at": str(t.get("date"))[:16].replace(" ", "T"),
                "symbol": t.get("ticker"),
                "action": t.get("action"),
                "units": t.get("units"),
                "price": t.get("price"),
                "charges": 0.0,
            }
            for t in list(self.session.transactions)[-40:]
        ]
        notes = [
            f"{mode.upper()} · run #{self.live.run_id} {self.live.config.name}: the chain is the "
            "deployment's live chain, the book is its portfolio, and Apply hands the basket to "
            "the run's own manual-order path.",
            "Marks are the run's own (LTP); fills are at the run's broker — paper at the touch, "
            "live through the LIMIT-at-touch ladder.",
        ]
        if not rows:
            notes.append(
                "No live chain: the run's quote source has no chain (cache source, or "
                "no broker session). Legs still show; the ladder needs a broker source."
            )
        return {
            "session": {
                "id": self.id,
                "mode": mode,
                "underlying": self.underlying,
                "lot_size": self._lot_size() or (legs[0]["lot_size"] if legs else 0),
                "date": today.isoformat(),
                "clock": now.strftime("%H:%M"),
                "range": [SESSION_OPEN.strftime("%H:%M"), SESSION_CLOSE.strftime("%H:%M")],
                "played_pct": round(100 * max(0.0, min(1.0, played)), 2),
                "capital": float(self.live.config.capital),
                "status": mode.upper(),
                "requires_confirm": True,
                "can_undo": False,
                "has_prev_day": False,
                "has_next_day": False,
                "run_id": self.live.run_id,
                "run_name": self.live.config.name,
                "strategy_id": self.live.config.strategy_id,
                "order_error": snap.get("order_error"),
            },
            "market": {
                "spot": spot,
                "fut": None,
                "carry": None,
                "prev_close": None,
                "day_open": None,
                "day_high": None,
                "day_low": None,
                "expiry": self.expiry,
                "dte": (date.fromisoformat(self.expiry) - today).days if self.expiry else None,
            },
            "chain": {
                "expiry": self.expiry,
                "expiries": [{"iso": e, "dte": (date.fromisoformat(e) - today).days} for e in exps],
                "atm_strike": next((r["strike"] for r in rows if r["atm"]), None),
                "lot_size": self._lot_size(),
                "listing_grid": False,
                "window": self.strike_window,
                "quoted": quoted,
                "total": 2 * len(rows),
                "rows": rows,
            },
            "legs": legs,
            "staged": (
                {
                    "items": self.staged["items"],
                    "label": self.staged["label"],
                    "after_legs": after,
                    "margin_before": margin,
                    "margin_after": m_after,
                    "margin_source": src_after,
                    "risk_after": risk_after,
                    "ticket": self.ticket(),
                }
                if self.staged
                else None
            ),
            "risk": {
                "realised": round(realised, 2),
                "unrealised": round(open_pnl, 2),
                "mtm": round(mtm, 2),
                "charges": 0.0,
                "margin": margin,
                "margin_source": src,
                "margin_detail": detail,
                "capital": float(self.live.config.capital),
                "legs_open": len(enabled),
                "greeks": greeks,
            },
            "fills": fills,
            "journal": [],
            "alerts": self._alerts_out(),
            "bookmarks": [],
            "cycle": None,
            "track": {"fills": [], "alerts": [], "bookmarks": []},
            "pricing": {
                "r": RISK_FREE,
                "q": 0.0,
                "t_floor_s": T_FLOOR_S,
                "expiry_time": EXPIRY_TIME.strftime("%H:%M"),
            },
            "notes": notes,
        }


# -------------------------------------------------------------------- registry
def open_console(run_id: int, *, expiry: str | None = None) -> LiveConsole:
    live = manager.get(int(run_id))
    if live is None:
        raise KeyError(f"no running deployment #{run_id}")
    with _LOCK:
        c = _CONSOLES.get(int(run_id))
        if c is None or c.live is not live:
            c = LiveConsole(live, expiry=expiry)
            _CONSOLES[int(run_id)] = c
        elif expiry:
            c.set_expiry(expiry)
        return c


def get_console(session_id: str) -> LiveConsole:
    run_id = int(str(session_id).split(":", 1)[1])
    with _LOCK:
        c = _CONSOLES.get(run_id)
    if c is None or manager.get(run_id) is not c.live:
        raise KeyError(session_id)
    return c


def drop_console(session_id: str) -> bool:
    run_id = int(str(session_id).split(":", 1)[1])
    with _LOCK:
        return _CONSOLES.pop(run_id, None) is not None


def recovering() -> bool:
    return bool(getattr(manager, "recovering", False))


def runs() -> list[dict]:
    """The DERIV deployments the console can drive, paper and live. PARTIAL while the
    manager is still recovering after a restart — see `recovering()`."""
    out = []
    for live in manager.list():
        if str(live.config.instrument_class).upper() != "DERIV" or not live.config.underlying:
            continue
        try:
            snap = live.snapshot() or {}
        except Exception:  # pragma: no cover
            snap = {}
        out.append(
            {
                "run_id": live.run_id,
                "name": live.config.name,
                "strategy_id": live.config.strategy_id,
                "mode": str(live.config.mode).upper(),
                "order_broker": snap.get("order_broker"),
                "underlying": str(live.config.underlying).upper(),
                "status": snap.get("status"),
                "open_positions": snap.get("open_positions"),
            }
        )
    return out


def is_live_id(session_id: str) -> bool:
    return str(session_id).startswith("live:")


_ = timedelta  # keep the import honest for callers that extend this module
