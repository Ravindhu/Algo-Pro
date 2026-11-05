"""
blue_engine
==========

Strategy framework built on the OpenAlgo Python SDK (openalgo==2.0.3).

Architecture
------------
Every sub-folder under Blue_engine/ is an ENGINE (plus core/, the shared kernel):
- alpha/        Alpha engine: signal generation. Also hosts signals/ (WHEN to trade)
- pov/          POV engine: execution. Also hosts slicers/ (HOW to slice orders)
- economics/    Economic engine
- market/       Market engine
- sectors/      Mansfield sector relative-strength engine
- portfolio/    Portfolio engine
- infrastructure/  Infrastructure engine
- core/         Shared kernel (SDK client, config, pipeline, runner, utils)
- input/        User-editable JSON configs (one per strategy run)

Runner entry point: ``python -m blue_engine.core.runner``

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
1. Drop a new file in alpha/signals/  (class MySignal(BaseSignal))
2. Drop a new file in pov/slicers/  (class MySlicer(BaseSlicer))   [optional, reuse POV]
3. Add Blue_engine/input/my_strategy.json
4. Run:  python -m blue_engine.core.runner Blue_engine/input/my_strategy.json
"""
# IMPORTANT: this must be the very first import. It fixes the import path
# so `from openalgo import api` always resolves to the installed SDK,
# not the empty local platform package at openalgo/openalgo/__init__.py.
from blue_engine import _sdkpath  # noqa: F401

__version__ = "0.1.0"
