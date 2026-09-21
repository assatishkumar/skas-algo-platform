"""`manual_options` — a deployment you trade by hand from the console (owner ask 2026-09-21).

"From the console I need an option to deploy to PAPER or LIVE and manage it as a manual
trading strategy." The run starts FLAT and in manual mode from its first tick: the console
lists it even while flat, every click stages, Commit places the legs through the run's
own manual-order path (paper broker, or LiveBroker behind every §1 gate on a LIVE run),
and the MANUAL RAIL — this class IS the rail (`ManualBookStrategy`) — runs the optional
rupee/percent target and stop, the hard time exit and the engine's expiry settlement.
Nothing here ever enters on its own: `on_slice` inherits the rail's exits only.

Distinct from a strategy run that was HANDED OVER after a hand-edit: that run keeps its
strategy paused behind the rail and can Resume once flat; a manual run has nothing to
resume, and its cycles are its own record. Deploy path: the console's "+ New manual run"
(`POST /console/manual-runs`) — see the allowlist in tests/test_deploy_registry.py.
"""

from __future__ import annotations

from skas_algo.strategies.manual_book import ManualBookStrategy


class ManualOptionsStrategy(ManualBookStrategy):
    strategy_id = "manual_options"

    def __init__(self, universe: list[str] | None = None, initial_capital: float = 0.0,
                 underlying: str | None = None, stop_pct: float = 0.0,
                 target_pct: float = 0.0, time_exit: str | None = None,
                 margin_anchor: float = 0.0, stop_amt: float = 0.0, target_amt: float = 0.0,
                 mark_basis: str = "exit", **_ignored) -> None:
        super().__init__(universe=universe, initial_capital=initial_capital,
                         underlying=underlying, stop_pct=stop_pct, target_pct=target_pct,
                         time_exit=time_exit, margin_anchor=margin_anchor,
                         stop_amt=stop_amt, target_amt=target_amt,
                         paused_strategy_id=None, handover_at=None,
                         handover_reason="manual_run", mark_basis=mark_basis)

    def exit_rules(self) -> list[str]:
        rules = super().exit_rules()
        # the rail's first line says "<strategy> paused since …" — a manual run paused nothing
        rules[0] = "Manual run — you build and manage the book from the console"
        return rules
