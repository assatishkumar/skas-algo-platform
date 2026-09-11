"""`manual_sim` — the Simulator's strategy id: a backtest whose decisions are the owner's.

A Simulator strategy (`services/simulator.py`) is an ordinary Algo + AlgoRun so Run detail,
Analyze and Compare render it unchanged, and an AlgoRun needs a registered strategy id.
This class exists to BE that id: it never decides anything (`on_slice` returns nothing), is
never deployed (no deploy card, no docs card — see the allowlists in
tests/test_deploy_registry.py and tests/test_strategy_docs_coverage.py) and is hidden from
the Runs list (`routes/backtest.py::list_runs`). The trades come from the console, cycle by
cycle, by hand.
"""

from __future__ import annotations

from skas_algo.engine.types import Signal


class ManualSimStrategy:
    strategy_id = "manual_sim"
    intraday = True

    def __init__(self, universe: list[str] | None = None, initial_capital: float = 0.0,
                 underlying: str | None = None, playbook: str | None = None,
                 capital_mode: str = "compound", **_ignored) -> None:
        self.underlying = (underlying or (universe[0] if universe else "NIFTY")).upper()
        self.initial_capital = float(initial_capital or 0.0)
        self.playbook = playbook
        self.capital_mode = capital_mode
        self.legs: list[dict] = []

    def on_slice(self, ctx) -> list[Signal]:      # the owner decides, never this class
        return []

    def export_state(self) -> dict:
        return {}

    def load_state(self, state: dict) -> None:
        return None
