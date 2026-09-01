"""Derived facts for the /portfolio screen — the money math, in one place.

The split with the frontend is deliberate. This module computes everything that needs a
LEDGER or a SOLVER (FIFO lots, cost basis, XIRR, per-lot tax regimes) and returns one record
per holding. Aggregates — allocation, drift, bucket targets, goal progress — are plain
arithmetic over those records and are done in the browser, so typing in a bucket-target box
updates every tile without a round trip. Keep it that way: nothing here should need to know
about buckets or the active tab.

**FIFO is not a preference.** Indian capital-gains rules read a sale against the oldest units
held, and the holding period that decides LTCG vs STCG is the LOT's, not the holding's. A
position built over four years and half sold has lots on both sides of the 12-month line at
once, so tax is computed per open lot and only then summed for display.

**A holding with no transactions is not a lesser holding.** PPF, EPF and a plot of land will
never have a trade history; their cost and value are typed. Both paths produce the same record
shape, and ``basis`` says which one produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from skas_algo.services.holdings import xirr

# ---------------------------------------------------------------- asset classes

# label / kind / default target % / colour. The colours are the design handoff's class
# palette and are echoed to the client so one definition drives chart, dot and donut.
ASSET_CLASSES: dict[str, dict] = {
    "stk": {"label": "Stocks · IN", "kind": "equity", "target": 22.0, "color": "#12b3a4"},
    "etf": {"label": "ETFs", "kind": "equity", "target": 8.0, "color": "#0d8a7e"},
    "mf": {"label": "Mutual funds", "kind": "equity", "target": 20.0, "color": "#0f9d63"},
    "us": {"label": "US stocks", "kind": "equity", "target": 10.0, "color": "#5b62e8"},
    "btc": {"label": "Crypto", "kind": "crypto", "target": 3.0, "color": "#8b90f2"},
    "bank": {"label": "Bank · FD", "kind": "debt", "target": 6.0, "color": "#66c29a"},
    "ppf": {"label": "PPF", "kind": "debt", "target": 8.0, "color": "#b07d10"},
    "epf": {"label": "EPF", "kind": "debt", "target": 10.0, "color": "#e8a13c"},
    "gold": {"label": "Gold", "kind": "gold", "target": 4.0, "color": "#c2661d"},
    "re": {"label": "Real estate", "kind": "realestate", "target": 9.0, "color": "#7a8a86"},
}

# The tag every holding carries, and the level rebalancing happens at. Deliberately coarser
# than asset class: a portfolio's real risk is "how much equity, how much debt", not "how many
# mid-cap funds". Crypto is its own tag rather than folded into equity or an "alternatives"
# catch-all — nothing else on the screen behaves like it, and averaging it into either hides
# exactly the position an investor most wants stated.
KINDS: tuple[str, ...] = ("equity", "debt", "gold", "realestate", "crypto")

KIND_LABELS = {
    "equity": "Equity", "debt": "Debt", "gold": "Gold",
    "realestate": "Real estate", "crypto": "Crypto",
}

KIND_COLORS = {
    "equity": "#12b3a4", "debt": "#e8a13c", "gold": "#c2661d",
    "realestate": "#7a8a86", "crypto": "#8b90f2",
}

KIND_TARGETS = {
    "equity": 55.0, "debt": 25.0, "gold": 10.0, "realestate": 7.0, "crypto": 3.0,
}

# Long-run assumptions behind the Goals tab's benchmark comparison. These are ASSUMPTIONS,
# not measurements — the UI states the rate next to every delta so the number is never read
# as an observed return. Editable in one place because that is the honest way to hold them.
BENCHMARKS: dict[str, float] = {
    "NIFTY 50 TRI": 12.0,
    "S&P 500 (INR)": 13.0,
    "Debt aggregate": 7.0,
    "Fixed 8%": 8.0,
}

# The equity LTCG exemption, applied ONCE across all equity long-term gains (FY26).
EQUITY_LTCG_EXEMPTION = 125_000.0
LTCG_RATE = 0.125
EQUITY_STCG_RATE = 0.20
SLAB_RATE = 0.30


def class_label(cls: str) -> str:
    return ASSET_CLASSES.get(cls, {}).get("label", cls)


def kind_of(cls: str, override: str | None = None) -> str:
    if override in KINDS:
        return override
    return ASSET_CLASSES.get(cls, {}).get("kind", "equity")


# ---------------------------------------------------------------- FIFO lots


@dataclass
class Lot:
    """An open parcel of units — what a sale would consume next and what tax reads."""

    on_date: date
    units: float
    cost_per_unit: float

    @property
    def cost(self) -> float:
        return self.units * self.cost_per_unit


@dataclass
class Disposal:
    """A closed parcel: units sold, matched back to the lot they came from."""

    sold_on: date
    bought_on: date
    units: float
    cost: float
    proceeds: float

    @property
    def gain(self) -> float:
        return self.proceeds - self.cost


@dataclass
class Ledger:
    lots: list[Lot] = field(default_factory=list)
    disposals: list[Disposal] = field(default_factory=list)
    flows: list[tuple[date, float]] = field(default_factory=list)
    oversold: float = 0.0  # units sold beyond anything bought — a data-entry error, surfaced
    actions: list[dict] = field(default_factory=list)  # splits/bonuses, with the ratio applied

    @property
    def units(self) -> float:
        return sum(lot.units for lot in self.lots)

    @property
    def cost(self) -> float:
        return sum(lot.cost for lot in self.lots)

    @property
    def realized(self) -> float:
        return sum(d.gain for d in self.disposals)


def _as_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def build_ledger(transactions: list[dict]) -> Ledger:
    """Walk buys and sells in date order, matching each sale to the oldest open lots.

    ``fees`` raise a buy's cost basis and reduce a sale's proceeds — the same rupee treated
    consistently on both sides, so a round trip's realized gain is net of what it cost to
    trade. Cashflows are recorded as they happen for XIRR: money out negative, money in
    positive.

    A sale with no lots left to consume is NOT silently dropped. It is counted in ``oversold``
    and surfaced, because the only ways to get one are a missing buy row or a typo — and both
    would otherwise show up as a mysteriously wrong cost basis months later.

    **A ``bonus`` row is a corporate action, and it RE-BASES the open lots — it does not add a
    new one.** ``units`` is how many shares appeared; the ratio is derived from what was held.
    Every open lot is multiplied out and its per-unit cost divided by the same factor, so total
    cost is unchanged, FIFO order is preserved, and each lot keeps its original date.

    Appending the free shares as a fresh lot instead — which is how a tracking sheet naturally
    records them — is what silently destroys the cost basis: the oldest lot keeps its PRE-split
    per-unit price while every later sale is of POST-split units, so FIFO matches a ₹903 sale
    against a ₹7,192 cost. On Bajaj Finance's June-2025 1:10 (4:1 bonus + 1:2 split) that
    invented a ₹10.3 L loss out of nothing.

    Simplification worth naming: bonus shares strictly carry NIL cost and their own allotment
    date for Indian capital-gains purposes, while a split apportions cost and inherits the
    original date. This models both as a proportional re-basing, which is exact for cost basis
    and P&L and can only differ on the holding period of the bonus portion — and only for a
    sale inside 12 months of the action."""
    led = Ledger()
    rows = sorted(transactions, key=lambda t: (_as_date(t["on_date"]), t.get("id") or 0))
    for t in rows:
        when = _as_date(t["on_date"])
        units = abs(float(t.get("units") or 0.0))
        price = float(t.get("price") or 0.0)
        fees = float(t.get("fees") or 0.0)
        if units <= 0:
            continue
        kind = str(t.get("kind", "buy")).lower()
        if kind == "bonus":
            held = led.units
            if held <= 0:
                # Free shares against nothing held: a buy row is missing before this date.
                # Recorded, not applied — applying it would divide by zero and inventing a
                # lot would put a fabricated cost basis into the tax lots.
                led.actions.append({
                    "on_date": when.isoformat(), "added": units, "ratio": None,
                    "problem": "no units held at this date — a buy is missing before it",
                })
                continue
            ratio = (held + units) / held
            for lot in led.lots:
                lot.units *= ratio
                lot.cost_per_unit /= ratio
            led.actions.append({
                "on_date": when.isoformat(), "added": units,
                "ratio": round(ratio, 6), "units_before": round(held, 4),
                "units_after": round(held + units, 4), "problem": None,
            })
            continue

        if kind == "buy":
            gross = units * price + fees
            led.lots.append(Lot(on_date=when, units=units, cost_per_unit=gross / units))
            led.flows.append((when, -gross))
            continue

        proceeds = units * price - fees
        led.flows.append((when, proceeds))
        remaining = units
        # Proceeds are apportioned by units so each disposal's gain is its own lot's.
        per_unit_proceeds = proceeds / units
        while remaining > 1e-9 and led.lots:
            lot = led.lots[0]
            take = min(lot.units, remaining)
            led.disposals.append(
                Disposal(
                    sold_on=when,
                    bought_on=lot.on_date,
                    units=take,
                    cost=take * lot.cost_per_unit,
                    proceeds=take * per_unit_proceeds,
                )
            )
            lot.units -= take
            remaining -= take
            if lot.units <= 1e-9:
                led.lots.pop(0)
        if remaining > 1e-9:
            led.oversold += remaining
    return led


# ---------------------------------------------------------------- tax


@dataclass
class Regime:
    label: str
    rate: float
    note: str
    equity_ltcg: bool = False  # eligible for the ₹1.25 L exemption
    loss_offset: bool = True  # crypto losses can't be set off


def regime_for(cls: str, kind: str, months_held: float) -> Regime:
    """FY26 rules, simplified — the same ladder the design handoff spells out, but fed a
    LOT's age rather than the holding's, so one position can sit in two regimes at once."""
    if cls in ("ppf", "epf"):
        return Regime("Exempt · EEE", 0.0, "Tax-free at withdrawal")
    if cls == "gold":
        return Regime("Exempt at maturity", 0.0, "SGB held to maturity → gain tax-free")
    if cls == "btc":
        return Regime("Flat 30%", SLAB_RATE, "No loss set-off allowed on VDAs", loss_offset=False)
    if cls == "bank":
        return Regime("Interest · slab", SLAB_RATE, "Interest taxed yearly at slab rate")
    if cls == "re":
        return Regime("LTCG 12.5%", LTCG_RATE, "Or 20% with indexation — pick lower")
    if cls == "us":
        if months_held >= 24:
            return Regime("LTCG 12.5%", LTCG_RATE, "Held > 24 months")
        return Regime("Slab rate", SLAB_RATE, "Held < 24 months — slab")
    if kind == "debt":
        return Regime("Slab rate", SLAB_RATE, "Debt fund — gains at slab")
    if months_held >= 12:
        return Regime("LTCG 12.5%", LTCG_RATE, "Held > 12 months", equity_ltcg=True)
    return Regime("STCG 20%", EQUITY_STCG_RATE, "Held < 12 months")


def months_between(start: date, end: date) -> float:
    return max((end.year - start.year) * 12 + (end.month - start.month), 0) + (
        (end.day - start.day) / 30.44
    )


# ---------------------------------------------------------------- per-holding record


def _annualised(cost: float, value: float, months: float) -> float | None:
    """Fallback return when there is no ledger to XIRR: cost → value over the period held.
    Correct only for a single lump sum, which is exactly the case it is used for."""
    if cost <= 0 or value <= 0 or months <= 0:
        return None
    return ((value / cost) ** (12.0 / months) - 1.0) * 100.0


def holding_view(holding: dict, transactions: list[dict], *, today: date | None = None) -> dict:
    """One holding's derived facts. ``transactions`` may be empty — see the module docstring.

    ``value`` follows units when a ledger exists (units × last price), because units are then
    a FACT and a typed value could contradict them. Without a ledger the typed value stands.
    """
    day = today or date.today()
    cls = holding["asset_class"]
    kind = kind_of(cls, holding.get("kind_override"))
    led = build_ledger(transactions)
    has_ledger = bool(transactions)

    last_price = holding.get("last_price")
    if has_ledger:
        units = led.units
        invested = led.cost
        value = units * float(last_price) if last_price else float(holding.get("value") or 0.0)
        first = min((lot_date for lot_date, _ in led.flows), default=None)
        buy_month = first.strftime("%Y-%m") if first else (holding.get("buy_month") or "")
    else:
        units = holding.get("units")
        invested = float(holding.get("invested") or 0.0)
        value = float(holding.get("value") or 0.0)
        buy_month = holding.get("buy_month") or ""

    # Age from the OLDEST open lot (ledger) or the stated buy month (summary).
    if has_ledger and led.lots:
        start = min(lot.on_date for lot in led.lots)
    elif buy_month:
        y, m = (int(x) for x in buy_month.split("-")[:2])
        start = date(y, m, 1)
    else:
        start = day
    age_months = months_between(start, day)

    # Return. A ledger gives a real money-weighted XIRR over actual cashflows; the closing
    # value is the final positive flow. A typed override always wins — a broker statement's
    # own figure beats anything derived here.
    if holding.get("xirr_pct") is not None:
        ret = float(holding["xirr_pct"])
        ret_basis = "stated"
    elif has_ledger:
        solved = xirr(led.flows + [(day, value)]) if value > 0 or led.disposals else None
        ret = round(solved * 100.0, 2) if solved is not None else None
        ret_basis = "xirr"
        if ret is None:  # degenerate flows (all one day, no sign change)
            ret = _annualised(invested, value, age_months)
            ret_basis = "annualised"
    else:
        ret = _annualised(invested, value, age_months)
        ret_basis = "annualised"
    if ret is not None:
        ret = round(ret, 2)

    # Per-lot unrealized tax. Aggregated for the row, but computed lot by lot.
    open_lots = led.lots if has_ledger else []
    if not open_lots and not has_ledger and units is not None and units > 0 and invested > 0:
        open_lots = [Lot(on_date=start, units=units, cost_per_unit=invested / units)]

    tax_est = 0.0
    ltcg = stcg = harvestable = 0.0
    exempt_gain = 0.0
    lot_rows: list[dict] = []
    if open_lots and value > 0:
        total_units = sum(lot.units for lot in open_lots) or 1.0
        for lot in open_lots:
            lot_value = value * (lot.units / total_units)
            gain = lot_value - lot.cost
            reg = regime_for(cls, kind, months_between(lot.on_date, day))
            if gain > 0:
                if reg.rate == 0:
                    exempt_gain += gain
                elif reg.equity_ltcg or reg.label.startswith("LTCG"):
                    ltcg += gain
                else:
                    stcg += gain
                tax_est += reg.rate * gain
            elif gain < 0 and reg.loss_offset:
                harvestable += -gain
            lot_rows.append({
                "on_date": lot.on_date.isoformat(),
                "units": round(lot.units, 4),
                "cost": round(lot.cost, 2),
                "value": round(lot_value, 2),
                "gain": round(gain, 2),
                "months_held": round(months_between(lot.on_date, day), 1),
                "regime": reg.label,
                "rate": reg.rate,
                "note": reg.note,
                "equity_ltcg": reg.equity_ltcg,
            })
    else:
        # No cost basis to tax (a bank balance, or a holding with nothing entered yet) — the
        # regime is still worth stating, so the Tax tab can explain why the bill is zero.
        reg = regime_for(cls, kind, age_months)
        lot_rows.append({
            "on_date": start.isoformat(), "units": 0.0, "cost": 0.0, "value": 0.0, "gain": 0.0,
            "months_held": round(age_months, 1), "regime": reg.label, "rate": reg.rate,
            "note": reg.note, "equity_ltcg": reg.equity_ltcg,
        })

    return {
        "id": holding.get("id"),
        "name": holding.get("name", ""),
        "asset_class": cls,
        "class_label": class_label(cls),
        "kind": kind,
        "kind_override": holding.get("kind_override"),
        "units": round(units, 4) if units is not None else None,
        "invested": round(invested, 2),
        "value": round(value, 2),
        "gain": round(value - invested, 2),
        "gain_pct": round((value - invested) / invested * 100.0, 2) if invested > 0 else None,
        "day_change": round(float(holding.get("day_change") or 0.0), 2),
        "xirr_pct": ret,
        "return_basis": ret_basis,
        "buy_month": buy_month,
        "age_months": round(age_months, 1),
        "sync": holding.get("sync", "manual"),
        "sync_source": holding.get("sync_source"),
        "sync_ref": holding.get("sync_ref"),
        "broker_account_id": holding.get("broker_account_id"),
        "last_price": last_price,
        "price_asof": holding.get("price_asof"),
        "native_currency": holding.get("native_currency"),
        "native_price": holding.get("native_price"),
        "native_invested": holding.get("native_invested"),
        "last_synced_at": holding.get("last_synced_at"),
        "units_locked": bool(holding.get("units_locked")),
        "broker_units": dict(holding.get("broker_units") or {}),
        "excluded_from_buckets": bool(holding.get("excluded_from_buckets")),
        "dividend_yield_pct": holding.get("dividend_yield_pct"),
        "note": holding.get("note"),
        "basis": "ledger" if has_ledger else "summary",
        "txn_count": len(transactions),
        "oversold_units": round(led.oversold, 4) if led.oversold > 1e-9 else 0.0,
        "realized": round(led.realized, 2),
        "disposals": [
            {
                "sold_on": d.sold_on.isoformat(),
                "bought_on": d.bought_on.isoformat(),
                "units": round(d.units, 4),
                "cost": round(d.cost, 2),
                "proceeds": round(d.proceeds, 2),
                "gain": round(d.gain, 2),
                "months_held": round(months_between(d.bought_on, d.sold_on), 1),
                "regime": regime_for(
                    cls, kind, months_between(d.bought_on, d.sold_on)
                ).label,
            }
            for d in led.disposals
        ],
        "tax": {
            "ltcg": round(ltcg, 2),
            "stcg": round(stcg, 2),
            "exempt": round(exempt_gain, 2),
            "harvestable": round(harvestable, 2),
            "estimate": round(tax_est, 2),
            "lots": lot_rows,
        },
    }


def apply_equity_exemption(rows: list[dict]) -> float:
    """Total estimated tax across holdings, with the ₹1.25 L equity-LTCG exemption applied
    ONCE — it is a per-taxpayer allowance, so subtracting it per holding would understate the
    bill by lakhs on a portfolio with several equity positions."""
    gross = sum(r["tax"]["estimate"] for r in rows)
    eligible = sum(
        lot["gain"]
        for r in rows
        for lot in r["tax"]["lots"]
        if lot.get("equity_ltcg") and lot["gain"] > 0
    )
    relief = min(eligible, EQUITY_LTCG_EXEMPTION) * LTCG_RATE
    return round(max(gross - relief, 0.0), 2)


# ---------------------------------------------------------------- income


def financial_year(iso: str) -> str:
    """India's FY runs 1 April to 31 March. Keying income on the calendar year would be wrong
    for nine months of every twelve."""
    y, m = int(iso[:4]), int(iso[5:7])
    start = y if m >= 4 else y - 1
    return f"FY{str(start + 1)[2:]}"


def income_view(rows: list[dict], dividends: list[dict], *, today: date | None = None) -> dict:
    """Distributions: what has arrived, and what the current book should throw off in a year.

    Expected income is DERIVED (value × yield), not stored — a forecast typed once ages into
    fiction the moment a position changes size, and this screen is read to plan against.
    Holdings with no yield entered are counted separately rather than as zero, because "we
    don't know" and "it pays nothing" lead to different decisions.
    """
    day = today or date.today()
    this_fy = financial_year(day.isoformat())

    by_holding: dict[int, list[dict]] = {}
    for d in dividends:
        by_holding.setdefault(d["holding_id"], []).append(d)

    lines, expected, unknown_value = [], 0.0, 0.0
    for r in rows:
        paid = by_holding.get(r["id"], [])
        fy_paid = sum(p["amount"] for p in paid if financial_year(p["on_date"]) == this_fy)
        total_paid = sum(p["amount"] for p in paid)
        yield_pct = r.get("dividend_yield_pct")
        annual = (r["value"] * yield_pct / 100.0) if yield_pct else None
        if annual is not None:
            expected += annual
        elif r["value"] > 0:
            unknown_value += r["value"]
        if not paid and annual is None:
            continue
        last = max((p["on_date"] for p in paid), default=None)
        lines.append({
            "holding_id": r["id"], "name": r["name"], "asset_class": r["asset_class"],
            "value": r["value"], "invested": r["invested"],
            "yield_pct": yield_pct,
            "expected_annual": round(annual, 2) if annual is not None else None,
            # Yield on COST is the number that says what the original decision now returns;
            # yield on value only ever says what today's price would buy.
            "yield_on_cost_pct": (
                round(annual / r["invested"] * 100.0, 2)
                if annual is not None and r["invested"] > 0 else None
            ),
            "received_fy": round(fy_paid, 2),
            "received_total": round(total_paid, 2),
            "last_paid": last,
            "payments": len(paid),
        })

    lines.sort(key=lambda x: -(x["expected_annual"] or 0))
    received_fy = sum(line["received_fy"] for line in lines)
    total_value = sum(r["value"] for r in rows)
    return {
        "fy": this_fy,
        "expected_annual": round(expected, 2),
        "expected_monthly": round(expected / 12.0, 2),
        "portfolio_yield_pct": round(expected / total_value * 100.0, 2) if total_value else 0.0,
        "received_fy": round(received_fy, 2),
        "received_total": round(sum(line["received_total"] for line in lines), 2),
        # How much of the book has no yield entered — the honest caveat on every figure above.
        "unpriced_value": round(unknown_value, 2),
        "unpriced_share_pct": (
            round(unknown_value / total_value * 100.0, 1) if total_value else 0.0
        ),
        "lines": lines,
    }


# ---------------------------------------------------------------- goals

DEFAULT_INFLATION_PCT = 6.0


def goal_schedule(goal: dict) -> list[dict]:
    """The goal's outflows as ``[{"year", "amount"}]`` in TODAY's rupees, year-ordered.

    Falls back to the legacy single target so an older goal reads as a one-row schedule
    rather than silently becoming unfunded."""
    rows = [
        {"year": int(r["year"]), "amount": float(r["amount"])}
        for r in (goal.get("schedule") or [])
        if r.get("year") and float(r.get("amount") or 0) > 0
    ]
    if not rows and goal.get("target_amount") and goal.get("target_year"):
        rows = [{"year": int(goal["target_year"]), "amount": float(goal["target_amount"])}]
    merged: dict[int, float] = {}
    for r in rows:
        merged[r["year"]] = merged.get(r["year"], 0.0) + r["amount"]
    return [{"year": y, "amount": round(a, 2)} for y, a in sorted(merged.items())]


def goal_projection(
    goal: dict,
    *,
    current_value: float,
    return_pct: float,
    today: date | None = None,
) -> dict:
    """Walk a goal year by year: grow the corpus, take out that year's (inflated) cost, and
    report the first year it runs dry.

    Two things this deliberately does NOT do. It does not compare a single number against a
    single target — a goal that is 'fully funded' in aggregate can still fail in 2031 because
    the money arrives too late. And it does not treat the entered amounts as nominal: they are
    today's prices, so each is carried forward at the goal's own inflation rate. School fees
    entered as 7 L in today's money are 11.2 L in 2034 at 6%, and planning against 7 L would
    understate the goal by a third.
    """
    day = today or date.today()
    base_year = day.year
    schedule = goal_schedule(goal)
    if not schedule:
        return {
            "schedule": [], "rows": [], "total_today": 0.0, "total_nominal": 0.0,
            "pv_required": 0.0, "funded_pct": None, "first_shortfall_year": None,
            "final_corpus": round(current_value, 2), "years": 0,
        }

    infl = float(goal.get("inflation_pct", DEFAULT_INFLATION_PCT)) / 100.0
    r = return_pct / 100.0
    sip = float(goal.get("monthly_sip") or 0.0)
    by_year = {row["year"]: row["amount"] for row in schedule}
    last_year = max(by_year)

    corpus = current_value
    rows: list[dict] = []
    first_short: int | None = None
    total_nominal = 0.0
    pv_required = 0.0
    shortfall_total = 0.0

    # The CURRENT year is already part-spent, so only what is left of it earns anything and
    # only the remaining SIPs are paid. Crediting a full year on 1 September would hand the
    # plan four months of growth and eight SIPs it never gets — flattering exactly the
    # near-term goals where being wrong matters most.
    months_left_now = 12 - day.month + 1

    for year in range(base_year, last_year + 1):
        n = year - base_year
        frac = (months_left_now / 12.0) if n == 0 else 1.0
        months = months_left_now if n == 0 else 12
        # Contributions land through the period, so they earn roughly half of its return —
        # crediting the full period would flatter every plan that leans on the SIP.
        corpus = corpus * ((1 + r) ** frac) + sip * months * (1 + r * frac / 2)
        today_amount = by_year.get(year, 0.0)
        needed = today_amount * ((1 + infl) ** n)
        total_nominal += needed
        pv_required += needed / ((1 + r) ** n) if r > -1 else needed

        # A shortfall is money you DON'T have, so it cannot compound at the investment return.
        # Letting the corpus go negative and grow did exactly that — a ₹15 L gap in 2027 became
        # ₹1.48 Cr of fictional debt by 2045, which would frighten someone out of a plan that
        # was merely one year tight. The gap is recorded, the corpus floors at zero, and the
        # SIP goes on rebuilding it.
        unmet = max(needed - corpus, 0.0)
        corpus = max(corpus - needed, 0.0)
        shortfall_total += unmet
        if unmet > 0 and first_short is None:
            first_short = year
        rows.append({
            "year": year,
            "amount_today": round(today_amount, 2),
            "amount_needed": round(needed, 2),
            "corpus_after": round(corpus, 2),
            "shortfall": round(unmet, 2),
            "short": unmet > 0,
        })

    total_today = sum(by_year.values())
    return {
        "schedule": schedule,
        "rows": rows,
        "total_today": round(total_today, 2),
        "total_nominal": round(total_nominal, 2),
        # What the whole stream is worth in today's money at the expected return — the single
        # honest answer to "how much would I need right now to be done with this".
        "pv_required": round(pv_required, 2),
        "funded_pct": round(current_value / pv_required * 100.0, 1) if pv_required > 0 else None,
        "first_shortfall_year": first_short,
        # The real gap, in the rupees of the years it occurs — not a compounded fiction.
        "shortfall_total": round(shortfall_total, 2),
        "final_corpus": round(corpus, 2),
        "years": last_year - base_year + 1,
    }
