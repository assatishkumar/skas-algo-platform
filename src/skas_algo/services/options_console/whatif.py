"""What-if: candidate adjustments to the book at the cursor, priced off the chain, compared
side by side — the console's "coach" (owner ask, 2026-09-15).

Deliberately DETERMINISTIC and free of prediction: every candidate is a hypothetical leg
list built from the current book (a roll, a wing, a partial close, a flatten, doing nothing)
and the numbers beside it are the same calculators the rail uses — the expiry payoff
(`payoff.metrics`: max P/L, breakevens, POP), the net greeks, the SPAN-shaped model margin
(the model on EVERY row, so the rows compare; the broker figure is not asked per candidate)
and the cash the adjustment moves. It ranks by max loss, finite before unlimited, and says
so. It never says which to take: it puts the trade-offs on one screen so the owner can.

A candidate whose new leg has not printed at the cursor is listed as refused with the
reason — the same rule as a preset: a price the market never printed cannot fill a leg."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from skas_algo.engine.options.charges import charges_for_txn

from . import payoff as _payoff

if TYPE_CHECKING:  # pragma: no cover
    from .session import ConsoleLeg, ConsoleSession


def _naked(shorts: list, longs: list) -> list:
    """Shorts with no long FURTHER OUT on the same right and expiry — the open tails."""
    out = []
    for s in shorts:
        covered = any(lg.right == s.right and lg.expiry == s.expiry and
                      ((s.right == "CE" and lg.strike > s.strike) or
                       (s.right == "PE" and lg.strike < s.strike)) and lg.lots >= s.lots
                      for lg in longs)
        if not covered:
            out.append(s)
    return out


def candidates(session: ConsoleSession, *, wing_steps: int = 2) -> dict:
    legs = [leg for leg in session.legs if leg.enabled]
    spot = session.market.index_spot(session.underlying) or 0.0
    at = session._minute_key()
    if not legs or spot <= 0:
        return {"at": at, "spot": spot or None, "candidates": [], "note": "no open book"}
    rows = session.chain_rows()
    step = session._grid(rows) if rows else 100.0
    shorts = [leg for leg in legs if leg.side == "S"]
    longs = [leg for leg in legs if leg.side == "B"]
    # the TESTED short: the one spot has moved INTO — a short CE is tested as spot rises
    # through it, a short PE as spot falls through it. "Nearest to spot" tied a straddle's
    # two strikes and named the CE while the PE was ₹300 in the money (owner, 2026-09-15).
    def tested_by(leg: ConsoleLeg) -> float:
        return (spot - leg.strike) if leg.right == "CE" else (leg.strike - spot)
    tested = max(shorts, key=tested_by) if shorts else None
    naked = _naked(shorts, longs)

    def away(leg: ConsoleLeg, n: int) -> float:
        """A strike n grid steps FURTHER from spot than this leg's."""
        return leg.strike + n * step if leg.right == "CE" else leg.strike - n * step

    specs: list[tuple[str, str, list[dict]]] = [("hold", "Do nothing", [])]
    if tested is not None:
        specs.append(("close_tested",
                      f"Close the tested short · {int(tested.strike)} {tested.right}",
                      [{"kind": "exit", "leg_id": tested.id, "lots": tested.lots}]))
        if tested.lots >= 2:
            specs.append(("close_half", f"Close half the tested short · {int(tested.strike)} "
                          f"{tested.right} ×{tested.lots // 2}",
                          [{"kind": "exit", "leg_id": tested.id, "lots": tested.lots // 2}]))
        for n in (1, 2):
            specs.append((f"roll_out_{n}", f"Roll the tested short {n} step{'s' if n > 1 else ''}"
                          f" out · {int(tested.strike)} → {int(away(tested, n))} {tested.right}",
                          [{"kind": "roll", "leg_id": tested.id, "strike": away(tested, n)}]))
    if naked:
        specs.append(("wing_naked", "Buy a wing on every naked short · "
                      + ", ".join(f"{int(away(s, wing_steps))} {s.right}" for s in naked),
                      [{"kind": "add", "right": s.right, "strike": away(s, wing_steps),
                        "side": "B", "lots": s.lots, "expiry": s.expiry} for s in naked]))
    if len(legs) > 1 or tested is None:
        specs.append(("flatten", f"Close all {len(legs)} legs", [{"kind": "flatten"}]))

    sigma = session.pop_sigma()
    out = []
    for cid, label, ops in specs:
        out.append(_evaluate(session, cid, label, ops, legs, spot, sigma))
    # rank: doing nothing first (the reference), then by max loss — finite before
    # unlimited, the smaller loss first; refused rows last
    ref, rest = out[0], out[1:]
    rest.sort(key=lambda c: (not c["ok"], c["max_loss"] is None,
                             -(c["max_loss"] or 0.0)))
    return {"at": at, "spot": round(spot, 2), "step": step,
            "tested": tested.id if tested else None, "candidates": [ref, *rest],
            "note": "ranked by max loss at expiry (finite before unlimited); margin is the "
                    "model on every row so the rows compare; the choice is yours"}


def _evaluate(session: ConsoleSession, cid: str, label: str, ops: list[dict],
              legs: list[ConsoleLeg], spot: float, sigma: float | None) -> dict:
    """Apply ``ops`` to a COPY of the book, price every new leg at the cursor, and
    measure the result. Nothing on the session changes."""
    book: list[ConsoleLeg] = [replace(leg) for leg in legs]
    by_id = {leg.id: leg for leg in book}
    cash = 0.0           # + = premium received by the adjustment, − = paid
    charges = 0.0
    realized = 0.0       # what the closes bank
    changes: list[str] = []
    try:
        for op in ops:
            k = op["kind"]
            if k == "exit":
                leg = by_id[op["leg_id"]]
                px = session._price(leg.right, leg.strike, leg.expiry)
                if px is None:
                    raise ValueError(f"{int(leg.strike)} {leg.right} has no price")
                n = int(op["lots"])
                units = n * leg.lot_size
                realized += (px - leg.entry) * units * leg.direction
                cash += (px * units) if leg.side == "B" else -(px * units)
                charges += charges_for_txn({"action": "SELL" if leg.side == "B" else "COVER",
                                            "amount": units * px})["total"]
                changes.append(f"{'SELL' if leg.side == 'B' else 'COVER'} {n}× "
                               f"{int(leg.strike)} {leg.right} @ {px:g}")
                if n >= leg.lots:
                    book.remove(leg)
                else:
                    leg.lots -= n
            elif k == "roll":
                leg = by_id[op["leg_id"]]
                px_old = session._price(leg.right, leg.strike, leg.expiry)
                px_new = session._price(leg.right, float(op["strike"]), leg.expiry)
                if px_old is None:
                    raise ValueError(f"{int(leg.strike)} {leg.right} has no price")
                if px_new is None:
                    raise ValueError(f"{int(op['strike'])} {leg.right} has no price")
                units = leg.units
                realized += (px_old - leg.entry) * units * leg.direction
                cash += ((px_old - px_new) if leg.side == "B" else (px_new - px_old)) * units
                charges += charges_for_txn({"action": "SELL" if leg.side == "B" else "COVER",
                                            "amount": units * px_old})["total"]
                charges += charges_for_txn({"action": "BUY" if leg.side == "B" else "SHORT",
                                            "amount": units * px_new})["total"]
                changes.append(f"ROLL {int(leg.strike)} → {int(op['strike'])} {leg.right} "
                               f"({px_old:g} → {px_new:g})")
                leg.strike = float(op["strike"])
                leg.symbol = f"{session.underlying}|{leg.expiry}|{int(leg.strike)}|{leg.right}"
                leg.entry = px_new
            elif k == "add":
                # a wing belongs on the SHORT's expiry, never the ladder's chip — with the
                # chip on the next expiry the wing landed a week later and the "fly" was a
                # diagonal (owner, 2026-09-15)
                expiry = op.get("expiry") or session.expiry or ""
                px = session._price(op["right"], float(op["strike"]), expiry)
                if px is None:
                    raise ValueError(f"{int(op['strike'])} {op['right']} {expiry} has no price")
                lot = session._lot_size() or 1
                n = int(op.get("lots", 1))
                units = n * lot
                side = op["side"]
                cash += (px * units) if side == "S" else -(px * units)
                charges += charges_for_txn({"action": "SHORT" if side == "S" else "BUY",
                                            "amount": units * px})["total"]
                changes.append(f"{'SHORT' if side == 'S' else 'BUY'} {n}× "
                               f"{int(op['strike'])} {op['right']} @ {px:g}")
                from .session import ConsoleLeg as _Leg
                book.append(_Leg(id=f"W{len(book)}",
                                 symbol=f"{session.underlying}|{expiry}|"
                                        f"{int(op['strike'])}|{op['right']}",
                                 right=op["right"], strike=float(op["strike"]),
                                 expiry=expiry, side=side, lots=n, lot_size=lot,
                                 entry=px, entered_at=session._minute_key()))
            elif k == "flatten":
                for leg in list(book):
                    px = session._price(leg.right, leg.strike, leg.expiry)
                    if px is None:
                        raise ValueError(f"{int(leg.strike)} {leg.right} has no price")
                    realized += (px - leg.entry) * leg.units * leg.direction
                    cash += (px * leg.units) if leg.side == "B" else -(px * leg.units)
                    charges += charges_for_txn({"action": "SELL" if leg.side == "B"
                                                else "COVER", "amount": leg.units * px})["total"]
                    changes.append(f"{'SELL' if leg.side == 'B' else 'COVER'} {leg.lots}× "
                                   f"{int(leg.strike)} {leg.right} @ {px:g}")
                book.clear()
            else:
                raise ValueError(f"unknown op {k!r}")
    except ValueError as exc:
        return {"id": cid, "label": label, "ops": ops, "ok": False, "reason": str(exc),
                "changes": changes, "cash": None, "charges": None, "max_profit": None,
                "max_loss": None, "breakevens": [], "be_dist_pct": None, "pop": None,
                "greeks": None, "margin": None, "margin_source": "model", "legs_after": []}
    legs_out = [session._leg_out(leg) for leg in book]
    risk = session._risk_out()
    offset = float(risk.get("realised") or 0.0) + realized - charges
    pay_legs = [{"right": x["right"], "strike": x["strike"], "direction": x["direction"],
                 "units": x["units"], "entry": x["entry"], "t": x.get("t"),
                 "iv": (x["iv"] / 100.0) if x.get("iv") else None} for x in legs_out]
    t = min((x["t"] for x in legs_out if x.get("t")), default=None)
    pay = _payoff.metrics(pay_legs, spot, offset=offset, sigma=sigma, t=t) if book else None
    greeks = session._net_greeks(legs_out) if book else None
    margin = session.margin(book, broker=False)[0] if book else 0.0
    return {"id": cid, "label": label, "ops": ops, "ok": True, "reason": None,
            "changes": changes, "cash": round(cash, 2), "charges": round(charges, 2),
            "realizes": round(realized, 2),
            "max_profit": pay["max_profit"] if pay else round(offset, 2),
            "max_loss": pay["max_loss"] if pay else round(offset, 2),
            "breakevens": pay["breakevens"] if pay else [],
            "be_dist_pct": pay["be_dist_pct"] if pay else None,
            "pop": pay["pop"] if pay else None,
            "greeks": greeks, "margin": round(margin, 0), "margin_source": "model",
            "legs_after": [f"{'S' if x['side'] == 'S' else 'B'} {int(x['strike'])} {x['right']} "
                           f"×{x['lots']}" for x in legs_out]}
