"""
blue_engine.dashboard.pages - Dash multi-page UI for the POV terminal.

Each module in this package exposes exactly one ``layout`` object (a
``dash.html.Div``). ``dash_app.py`` registers them with Dash Pages, so
the URL routing and the left-hand nav are handled by Dash itself.

Pages
-----
    terminal       Page 1: Main runtime, live band, order entries
    portfolio      Page 2: Live OMS/EMS positions and fills
    risk_control   Page 3: Risk-engine matrix and guard limits
    analytics      Page 4: Alpha analytics + backtest/statistics
    economic       Page 5: Macro regime engine (gauges, signals, history)
    infrastructure Page 6: System health and raw logs

Design notes
------------
* All pages are **read-only**. They render whatever the running engine
  or the ledger/config store already produced; nothing here places an
  order. That keeps the dashboard safe to leave open.
* Data access goes through :mod:`blue_engine.dashboard.pages._data`, the
  single adapter that knows how to read the config, the ledger and any
  live engine snapshot. Pages never import ``openalgo`` directly, so the
  whole UI still imports cleanly on a machine with no broker.
"""
from __future__ import annotations

# Dash Pages discovers these by module path (dash.register_page), not by
# package import, so we intentionally do not eagerly import them here.
# This keeps `import blue_engine.dashboard.pages` cheap and side-effect free.

__all__ = [
    "terminal",
    "portfolio",
    "risk_control",
    "analytics",
    "economic",
    "infrastructure",
]
