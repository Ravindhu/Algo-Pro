"""
run_pov.py -- POV engine launcher.

COMPLETE STEP-BY-STEP GUIDE:
============================

STEP 1: Start OpenAlgo Main Application
----------------------------------------
Open PowerShell in c:\\Users\\KanagalakshmiM\\openalgo and run:

    .\\start.sh

This starts:
  - WebSocket proxy on port 8765
  - Main Flask app on port 5000
  - Database migrations automatically

After starting, verify by opening: http://127.0.0.1:5000
You should see the OpenAlgo login/dashboard.

STEP 2: Get Your API Key
------------------------
In the OpenAlgo UI (http://127.0.0.1:5000):
  1. Go to Settings -> API Keys
  2. Generate a new API key or copy an existing one
  3. Copy the API key (you'll need it below)

STEP 3: Set Environment Variables
---------------------------------
In PowerShell, set your API key:

    $env:OPENALGO_API_KEY = "your-api-key-here"
    $env:OPENALGO_URL = "http://127.0.0.1:5000"

STEP 4: Run This Script
-----------------------
    uv run python 
    run_pov.py

This starts the POV Engine (client-side strategy):
  - Loads USER_CONFIG below
  - Connects to running OpenAlgo server via REST API
  - Enables signals/slicers based on USER_CONFIG
  - Runs complete strategy loop (signal detection + POV execution)

STEP 5: Monitor Your Execution
-----------------------------
While the script runs, you can check:
  - OpenAlgo UI: Orders -> Order Book (filter by strategy_label)
  - Terminal output: Shows real-time execution progress

TROUBLESHOOTING:
----------------
- "Connection refused": Make sure OpenAlgo main app is running (Step 1)
- "Invalid API key": Verify OPENALGO_API_KEY matches OpenAlgo UI
- "Insufficient funds": Check available cash in OpenAlgo UI
- "Symbol not found": Verify symbol/exchange are correct (NSE/BSE only)

USER CONFIGURATION:
-------------------
Edit the USER_CONFIG dictionary below to change trade parameters.
All parameters are documented with inline comments.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# â”€â”€ USER_CONFIG â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
USER_CONFIG = {

    # â”€â”€ IDENTITY â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    "strategy_label": "POV-INFY-NSE",  # shown in OpenAlgo orderbook
    "api_key": None,                    # None=from OPENALGO_API_KEY env
    "host": None,                       # None=http://127.0.0.1:5000
    "ws_url": None,                     # order-update WS; None=default

    # â”€â”€ SYMBOL â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    "symbol":   "INFY",
    "exchange": "NSE",

    # â”€â”€ STRATEGY MODE â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    # Toggle each signal/slicer. Enable ocl_keylevel for OCL pattern signals.
    "strategies": {"keylevel": False, "ocl_keylevel": False, "pov": True},

    # â”€â”€ KEYLEVEL SIGNAL (when strategies.keylevel=True) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    "keylevel": {
        "trigger_price":     1480.00,   # price to watch
        "trigger_direction": "ABOVE",    # ABOVE or BELOW
        "poll_seconds":        5,        # seconds between checks
        "tolerance":          0.0,        # buffer around trigger_price
        "one_shot":         True,          # fire once then stop
        "wait_seconds":        1800,       # max wait before giving up
        "action":           "BUY",         # BUY or SELL
        "quantity":          1000,         # shares to trade on trigger
    },

    # â”€â”€ OCL KEYLEVEL SIGNAL (when strategies.ocl_keylevel=True) â”€â”€â”€
    
    "ocl_keylevel": {
        # [HTF SETTINGS] Higher Timeframe Configuration
        "htf_interval":         "WEEKLY",   # HTF candle interval
        "htf_candles":          5,            # candles to fetch for HTF scan

        # [PATTERN SETTINGS] Candlestick Pattern Parameters
        "pattern_type":         "RRG",         # RRG (Red-Red-Green), BUY-only; GRR removed
        "min_body_pct":         0.3,          # min body/range ratio (noise filter)
        "trade_direction":      "AUTO",        # AUTO | BUY | SELL

        # [LTF SETTINGS] Lower Timeframe Monitoring
        "ltf_interval":         "240 minute",    # LTF candle interval
        "ltf_touch_buffer":     0.05,         # tolerance around trigger level

        # [TRADE SETTINGS] Execution Parameters
        "action":               "BUY",         # BUY or SELL
        "quantity":             1000,         # TOTAL position size (parent budget)

        # [MONITORING SETTINGS] Polling and Timeout Controls
        "poll_seconds":         10,           # seconds between LTF checks
        "wait_seconds":         1800,         # max wait before giving up
        "one_shot":             True,         # legacy flag (use exec_mode instead)

        # [EXECUTION SETTINGS] Per-Run Slicing Behavior (USER-DEFINED)
        # Controls how many times the signal can fire and how much qty per fire.
        #   ONE_SHOT:        fire once, then stop (legacy default)
        #   CONTINUOUS:      fire on every touch (cooldown enforced)
        #   COUNT_LIMITED:   fire up to max_fires times
        #   AUTO:            fire once per new pattern, re-arm on rearm_on_fire
        "exec_mode":            "ONE_SHOT",    # execution mode
        "qty_per_fire":         200,           # shares/contracts per individual fire
        "max_fires":            5,             # max fires (COUNT_LIMITED mode)
        "cooldown_seconds":     60,            # min seconds between fires
        "rearm_on_fire":        False,         # re-scan HTF for new pattern after fire
    },

    # â”€â”€ POV SLICER â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    "pov": {
        # CORE EXECUTION
        "action":                    "BUY",       # BUY or SELL
        "quantity":               100000,          # total parent shares
        "participation_pct":          0.5,       # % of interval volume per slice
        "duration_seconds":           900,         # 900=15min | 1800=30min | 3600=1hr
        "poll_interval_seconds":       15,         # seconds between volume checks
        # ORDER PARAMETERS
        "product":                 "CNC",          # CNC | INTRADAY | MIS
        "order_mode":             "LIMIT",         # LIMIT or MARKET
        "limit_price":              None,            # None=depth-aware; float=fixed
        "max_slice_quantity":       100,            # cap each child order
        "ceiling_price":         1500.0,          # pause BUY if LTP >= this
        "stop_loss_price":       1450.0,          # exit all if LTP <= this
        # TIME GUARDS (IST HH:MM)
        "exit_participation_pct":   30.0,          # abort if POV rate > this %
        "limit_expiry_pct":         80.0,         # switch LIMIT->MARKET at this % of interval
        "cutoff_time":          "15:15",            # switch to MARKET at this IST time
        "squareoff_time":        "15:20",            # cancel+square-off at this IST time
        # DEPTH-AWARE PRICING
        "depth_aware_pricing":      True,           # True=live bid/ask; False=ceiling_price
        "depth_offset_ticks":          1,            # 0=aggressive 1=normal 2=passive
        "tick_size":                None,            # None=auto-detect from symbol master
        "depth_slice_cap":           True,           # enforce min qty per depth level
        "depth_slice_min_qty":          1,           # skip level if depth qty < this
        # CIRCUIT BREAKER
        "circuit_guard_enabled":    True,            # pause near daily upper/lower limit
        "circuit_buffer_pct":        0.5,           # buffer % from circuit to pause
        # EXCHANGE & COMPLIANCE
        "sme_periodic_call_auction": False,        # True for SME / periodic call auction scrips
        "auction_window_enabled":    True,           # respect auction windows
        "auction_windows": ["09:15-15:30"],       # list of "HH:MM-HH:MM" windows
        "auction_price_enabled":    True,            # use broker auction price
        "lot_size":                 None,           # None=auto-detect; rounds qty to lot
        "restricted_same_day_reverse_exit": False,   # block SELL after BUY fill today
        "execution_trade_date":     None,           # YYYY-MM-DD; None=today
        "supervisor_override":      False,            # True=bypass risk supervisor gates
        # SYSTEM STABILITY
        "max_order_to_trade_ratio":  10.0,           # pause if open/filled >= this
        "min_order_interval_seconds":  1.0,          # seconds between orders; 0=off
        "cancellation_cool_off_seconds": 2.0,        # wait after cancel before new order
        "quote_failure_threshold":       3,            # abort after this many quote failures
        # VWAP VALUE FILTER
        "vwap_filter_enabled":       True,             # True=dynamic POV rate; False=fixed
        "vwap_ceiling_pct":           0.5,           # raise rate if LTP > buyer VWAP + this %
        "vwap_floor_pct":             3.0,           # lower rate if LTP < buyer VWAP - this %
        "min_pov_pct":                5.0,           # hard floor on participation %
        "max_pov_pct":               25.0,           # hard ceiling on participation %
        "passthrough_floor":   "part_pct",             # "part_pct" | "vwap_pct" | float
        "vwap_session_floor_price": None,             # pause if LTP drops below this price
        # SLIPPAGE FEEDBACK LOOP
        "slippage_guard_enabled":    True,              # reduce POV rate on poor fills
        "max_slippage_bps":          50.0,            # slippage threshold bps (50=0.50%)
        "slippage_penalty_factor":    0.5,            # multiply rate by this on penalty
        "slippage_window_fills":        10,            # fills to average for slippage calc
        "arrival_window_ticks":         20,           # LTP ticks for arrival VWAP
    },
}
# ENGINE BOOTSTRAP -- do not edit below
# ===============================================================

def _find_project_root(start):
    from pathlib import Path
    for p in [Path(start), *Path(start).parents]:
        if (p / "blue_engine").is_dir():
            return str(p)
    return str(Path(start))

_ROOT = _find_project_root(__file__)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_api = USER_CONFIG.get("api_key")
if _api:
    os.environ.setdefault("OPENALGO_API_KEY", _api)


def main():
    from blue_engine.core.runner import run_with_config
    print("=" * 68)
    print("POV ENGINE  --  single-file launcher")
    print("=" * 68)
    print("project root : " + _ROOT)
    s = USER_CONFIG.get("strategies", {})
    print("strategies   : " + str(list(s.items())))
    sym = str(USER_CONFIG.get("symbol"))
    ex  = str(USER_CONFIG.get("exchange"))
    print("symbol       : " + sym + "/" + ex)
    print("=" * 68)
    return run_with_config(USER_CONFIG)


if __name__ == "__main__":
    sys.exit(main())
