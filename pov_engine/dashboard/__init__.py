"""
pov_engine.dashboard - live OCL trading dashboard.

A terminal-rendered (rich) dashboard that:

  1. Subscribes to the OCL screener (reads OclScreenerMatch from the
     DB; new matches surface on the dashboard).
  2. Polls live quotes for every watched symbol via
     services.quotes_service.get_quotes.
  3. Runs a per-symbol POV state machine
     (WATCHING -> ACCUMULATING -> SLOW / STOPPED / EXITED).
  4. Tracks virtual positions and reports realized + unrealized
     P&L. By default this is a PAPER simulator; set LIVE_TRADING=1
     in the env to flip into a real-trading mode (not wired by
     default - the user must opt in explicitly).

Architecture
------------
  pov_state_machine.py   Per-symbol POV state machine. Pure Python.
  position_tracker.py    Virtual ledger (qty, avg price, P&L).
  data_source.py         Async event loop: quotes + screener feed.
  candlestick.py         Unicode-block ASCII candle sparkline.
  render.py              rich Layout / Table / Panel rendering.
  app.py                 LiveDashboard class - the orchestrator.
  cli.py                 argparse entry point + key bindings.
"""
from .pov_state_machine import PovState, PovStateMachine, PovConfig
from .position_tracker import PositionTracker, VirtualPosition, TradeRecord
from .candlestick import render_candles, candle_sparkline
from .app import LiveDashboard, DashboardConfig

__all__ = [
    "DashboardConfig",
    "LiveDashboard",
    "PovConfig",
    "PovState",
    "PovStateMachine",
    "PositionTracker",
    "TradeRecord",
    "VirtualPosition",
    "candle_sparkline",
    "render_candles",
]