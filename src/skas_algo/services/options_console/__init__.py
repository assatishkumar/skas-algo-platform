"""The Options Console: an interactive, minute-stepped options screen.

Deliberately OUTSIDE ``live/``. In replay this package owns its own book and touches no
broker, no order path and no trading DB — the ``/portfolio`` precedent (CLAUDE.md §8a), and
``tests/test_options_console.py`` pins it. Live mode (a later phase) does not change that:
it drives an EXISTING ``LiveRun`` through the already-gated ``manual_order`` path rather
than growing a second way to place an order.
"""
