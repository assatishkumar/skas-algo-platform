"""Headline performance metrics, computed from a RunResult.

Formulas mirror skas-trading's SST get_metrics so BACKTEST mode is verifiably
faithful (see tests/test_sst_parity.py).
"""

from __future__ import annotations

from .runner import RunResult


def compute_metrics(result: RunResult, initial_capital: float) -> dict:
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
    for day in history:
        eq = day["total_equity"]
        hwm = max(hwm, eq)
        dd = (hwm - eq) / hwm if hwm > 0 else 0.0
        max_dd = max(max_dd, dd)
        max_invested = max(max_invested, day.get("invested_capital", 0.0))

    sells = [t for t in result.transactions if t["action"] == "SELL"]
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
    gross_profit = sum(t["profit"] for t in sells if t["profit"] > 0)
    profitable = sum(1 for t in sells if t["profit"] > 0)

    return {
        "Total Return %": total_return,
        "CAGR %": cagr * 100,
        "Final Equity": final_equity,
        "Max Drawdown %": max_dd * 100,
        "Max Capital Used": max_invested,
        "Total Trades": total_trades,
        "Win Rate %": win_rate,
        "Cash Balance": cash_balance,
        "Total Withdrawals": total_withdrawals,
        "Total Taxes": total_taxes,
        "Avg Monthly Profit Booking": profitable / months,
        "Avg Monthly Profit (Pre-Tax)": gross_profit / months,
        "Avg Monthly Profit (Post-Tax)": (gross_profit - total_taxes) / months,
    }
