"""Headline performance metrics, computed from a RunResult.

Formulas mirror skas-trading's SST get_metrics so BACKTEST mode is verifiably
faithful (see tests/test_sst_parity.py).
"""

from __future__ import annotations

from .runner import RunResult


def compute_metrics(
    result: RunResult, initial_capital: float, *, deployed: bool = False, idle_return: float = 0.0
) -> dict:
    history = result.history
    if not history:
        return {}

    final = history[-1]
    start_date = history[0]["date"]
    end_date = final["date"]
    years = (end_date - start_date).days / 365.25
    final_equity = final["total_equity"]

    hwm = 0.0
    max_dd = 0.0
    max_invested = 0.0
    max_margin = 0.0  # peak short-option margin blocked (options runs only)
    for day in history:
        eq = day["total_equity"]
        hwm = max(hwm, eq)
        dd = (hwm - eq) / hwm if hwm > 0 else 0.0
        max_dd = max(max_dd, dd)
        max_invested = max(max_invested, day.get("invested_capital", 0.0))
        max_margin = max(max_margin, day.get("margin_used", 0.0))

    # Realized-P&L events: long sells, short buy-to-close (COVER), and expiry settlement
    # (SETTLE). For an equity-only run there are no COVER/SETTLE events, so this is
    # identical to filtering on "SELL" alone (parity preserved).
    sells = [t for t in result.transactions if t["action"] in ("SELL", "COVER", "SETTLE")]
    wins = sum(1 for t in sells if t["profit"] > 0)
    total_trades = len(sells)
    win_rate = (wins / total_trades * 100) if total_trades else 0.0

    portfolio = result.portfolio
    total_taxes = portfolio.total_taxes if portfolio else 0.0
    total_withdrawals = portfolio.total_withdrawals if portfolio else 0.0
    cash_balance = portfolio.cash if portfolio else 0.0

    # Investor-return convention (consistent across Total Return and CAGR):
    # withdrawals are added back (your distributions, not losses), taxes are a real
    # cost already reflected in final equity. So both metrics share this base value.
    total_value = final_equity + total_withdrawals
    total_return = (total_value - initial_capital) / initial_capital * 100
    cagr = 0.0
    if years > 0 and total_value > 0:
        cagr = (total_value / initial_capital) ** (1 / years) - 1

    # Average monthly figures over the months the run spans.
    months = max(
        1,
        (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month) + 1,
    )
    # NOTE: gross_profit/profitable count WINNERS ONLY (parity with SST). On a losing
    # run these "Avg Monthly Profit*" figures still look positive — misleading on their
    # own — so we ALSO expose net realized P&L (winners minus losers) below. The gross
    # keys are kept byte-identical because test_sst_parity pins them.
    gross_profit = sum(t["profit"] for t in sells if t["profit"] > 0)
    profitable = sum(1 for t in sells if t["profit"] > 0)
    net_profit = sum(t["profit"] for t in sells)  # winners + losers = honest figure

    out = {
        "Total Return %": total_return,
        "CAGR %": cagr * 100,
        "Final Equity": final_equity,
        "Max Drawdown %": max_dd * 100,
        "Max Capital Used": max_invested,
        "Max Margin Used": max_margin,
        "Total Trades": total_trades,
        "Win Rate %": win_rate,
        "Cash Balance": cash_balance,
        "Total Withdrawals": total_withdrawals,
        "Total Taxes": total_taxes,
        "Avg Monthly Profit Booking": profitable / months,
        "Avg Monthly Profit (Pre-Tax)": gross_profit / months,
        "Avg Monthly Profit (Post-Tax)": (gross_profit - total_taxes) / months,
        # Net realized P&L (winners − losers) — the figures the dashboard surfaces.
        "Net Realized P&L": net_profit,
        "Avg Monthly Net P&L (Pre-Tax)": net_profit / months,
        "Avg Monthly Net P&L (Post-Tax)": (net_profit - total_taxes) / months,
    }
    if deployed:
        out.update(_deployed_idle_metrics(history, years, total_value, initial_capital, idle_return))
    return out


def _deployed_idle_metrics(history, years, total_value, initial_capital, idle_return) -> dict:
    """Return-on-deployed-capital + idle-cash overlay (reporting only; the equity curve and the
    standard CAGR are untouched). Deployed base = AVERAGE daily long cost basis; idle cash (the
    daily cash balance) is assumed to compound at ``idle_return``/yr."""
    profit = total_value - initial_capital
    deployed_daily = [d.get("invested_capital", 0.0) for d in history]
    avg_deployed = sum(deployed_daily) / len(deployed_daily) if deployed_daily else 0.0

    out: dict = {"Avg Deployed Capital": avg_deployed}
    if avg_deployed > 0:
        # Lifetime (cumulative) return measured against the average rupees actually at work.
        out["Return on Deployed Capital %"] = profit / avg_deployed * 100
        if years > 0:
            # Per-year return on deployed capital, SIMPLE (arithmetic) annualization.
            # A geometric CAGR — (1 + profit/avg_deployed)^(1/years) − 1 — is invalid here:
            # `profit` is cumulative against the INITIAL capital while `avg_deployed` is a time
            # average that grows with equity-scaled sizing, so over many years the geometric root
            # crushes the figure below the true CAGR. The simple per-rupee-year yield is coherent.
            out["Deployed Return %/yr"] = profit / avg_deployed / years * 100

    # Idle cash compounding at idle_return/yr over each inter-day interval.
    idle_interest = 0.0
    if idle_return > 0:
        daily_rate = (1 + idle_return) ** (1 / 365.25) - 1
        for i in range(1, len(history)):
            gap = (history[i]["date"] - history[i - 1]["date"]).days or 1
            growth = (1 + daily_rate) ** gap
            idle_interest = idle_interest * growth + history[i - 1].get("cash", 0.0) * (growth - 1)
        out["Idle Interest (assumed)"] = idle_interest
        idle_total = total_value + idle_interest
        if years > 0 and idle_total > 0:
            label = f"CAGR (idle @ {round(idle_return * 100)}%) %"
            out[label] = ((idle_total / initial_capital) ** (1 / years) - 1) * 100
    return out
