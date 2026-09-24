"""
pov_engine
==========

Strategy framework built on the OpenAlgo Python SDK (openalgo==2.0.3).

Architecture
------------
- core/        Shared OpenAlgo-style utilities (SDK client init, mode read, time)
- signals/     Signal strategies (decide WHEN to trade)
- slicers/     Execution algorithms (decide HOW to slice orders)
- input/       User-editable JSON configs (one per strategy run)
- runner.py    Entry point -- picks a (signal, slicer) pair from input JSON

Design rules (from the OpenAlgo reference strategy simple_ema_strategy.py)
-------------------------------------------------------------------------
- Import:  from openalgo import api
- Init:    client = api(api_key=..., host=..., ws_url=...)
- All SDK method args are keyword-only (placeorder, placesmartorder, etc.)
- Read OPENALGO_API_KEY, HOST_SERVER, WEBSOCKET_URL from environment
- Mode (LIVE vs ANALYZER) is controlled in the OpenAlgo UI, never toggled here
- Every response is checked:  if response.get("status") == "success":

Adding a new strategy
---------------------
1. Drop a new file in signals/  (class MySignal(BaseSignal))
2. Drop a new file in slicers/  (class MySlicer(BaseSlicer))   [optional, reuse POV]
3. Add pov_engine/input/my_strategy.json
4. Run:  python -m pov_engine.runner pov_engine/input/my_strategy.json
"""
# IMPORTANT: this must be the very first import. It fixes the import path
# so `from openalgo import api` always resolves to the installed SDK,
# not the empty local platform package at openalgo/openalgo/__init__.py.
from pov_engine import _sdkpath  # noqa: F401

__version__ = "0.1.0"
