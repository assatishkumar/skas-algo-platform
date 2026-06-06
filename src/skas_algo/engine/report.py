"""Build an extensive report (scalars + monthly/yearly breakdowns) from a RunResult.

Breakdown logic mirrors skas-trading's SST get_metrics so reports match the
familiar backtest output.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from .jsonutil import to_native
from .metrics import compute_metrics
from .runner import RunResult


def build_report(result: RunResult, initial_capital: float) -> dict[str, Any]:
    metrics = compute_metrics(result, initial_capital)
    if not result.history:
        return {"metrics": metrics}

    daily = pd.DataFrame(result.history)
    daily["year"] = daily["date"].dt.year
    daily["month"] = daily["date"].dt.month
    years = sorted(daily["year"].unique())
    flush_log = result.monthly_flush_log

    # Per-year tax/withdrawal totals from the monthly flush log.
    yearly_tax = {y: 0.0 for y in years}
    yearly_wd = {y: 0.0 for y in years}
    for (y, _m), entry in flush_log.items():
        if y in yearly_tax:
            yearly_tax[y] += entry["tax"]
            yearly_wd[y] += entry["withdrawal"]

    monthly_profit = {y: {m: 0.0 for m in range(1, 13)} for y in years}
    for t in result.transactions:
        if t["action"] == "SELL":
            y, m = t["date"].year, t["date"].month
            if y in monthly_profit:
                monthly_profit[y][m] += t["profit"]

    monthly_capital = {y: {m: 0.0 for m in range(1, 13)} for y in years}
    for (y, m), cap in daily.groupby(["year", "month"])["invested_capital"].max().items():
        monthly_capital[y][m] = cap

    monthly_equity = {y: {m: 0.0 for m in range(1, 13)} for y in years}
    for (y, m), eq in daily.groupby(["year", "month"])["total_equity"].last().items():
        monthly_equity[y][m] = eq

    yearly = {}
    for i, year in enumerate(years):
        ydf = daily[daily["year"] == year]
        if i == 0:
            start_capital = initial_capital
        else:
            prev = daily[daily["year"] == year - 1]
            start_capital = prev.iloc[-1]["total_equity"] if not prev.empty else initial_capital

        end_value = ydf.iloc[-1]["total_equity"]
        w, t = yearly_wd.get(year, 0.0), yearly_tax.get(year, 0.0)
        total_val = end_value + w + t
        abs_return = total_val - start_capital
        pct_return = (abs_return / start_capital * 100) if start_capital > 0 else 0.0

        # Drawdown within the year, adjusting for cumulative withdrawals/taxes.
        events = [
            {"date": e["date"], "withdrawal": e["withdrawal"], "tax": e["tax"]}
            for (ey, _m), e in flush_log.items()
            if ey == year
        ]
        hwm = start_capital
        max_dd = 0.0
        for _, row in ydf.iterrows():
            cw = sum(e["withdrawal"] for e in events if e["date"] <= row["date"])
            ct = sum(e["tax"] for e in events if e["date"] <= row["date"])
            tv = row["total_equity"] + cw + ct
            hwm = max(hwm, tv)
            dd = (hwm - tv) / hwm if hwm > 0 else 0.0
            max_dd = max(max_dd, dd)

        yearly[int(year)] = {
            "Return (Abs)": abs_return,
            "Return (%)": pct_return,
            "Portfolio Value": end_value,
            "Withdrawals": w,
            "Taxes": t,
            "Max Drawdown (%)": max_dd * 100,
            "Max Capital Used": ydf["invested_capital"].max(),
        }

    equity_curve = [
        {"date": row["date"].strftime("%Y-%m-%d"), "equity": float(row["total_equity"])}
        for row in result.history
    ]

    return to_native(
        {
            "metrics": metrics,
            "yearly": yearly,
            "monthly_profit": {int(y): v for y, v in monthly_profit.items()},
            "monthly_capital": {int(y): v for y, v in monthly_capital.items()},
            "monthly_equity": {int(y): v for y, v in monthly_equity.items()},
            "equity_curve": equity_curve,
        }
    )
