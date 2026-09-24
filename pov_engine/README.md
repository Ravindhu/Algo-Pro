# pov_engine

A strategy framework built on the [OpenAlgo Python SDK](https://pypi.org/project/openalgo/) (`openalgo==2.0.3`).

## Architecture

```
pov_engine/
  core/           OpenAlgo SDK style helpers (client init, mode read, time)
  signals/        Signal strategies (decide WHEN to trade)
    base.py       Abstract base
    manual.py     Always emits the configured intent
    _template.py  Skeleton for your own signals (engulfing, breakout, etc.)
  slicers/        Execution algorithms (decide HOW to slice orders)
    base.py       Abstract base
    pov.py        Percentage-of-Volume slicer (all 14 user config fields)
  input/          User-editable JSON configs (no Python needed to edit)
  runner.py       Entry point: python -m pov_engine.runner
```

## Design rules (mirroring the OpenAlgo reference)

These come from the official OpenAlgo example `strategies/Custom Statergy/simple_ema_strategy.py`:

- `from openalgo import api`
- `client = api(api_key=..., host=..., ws_url=...)`
- All SDK method calls use **keyword-only** arguments
- Read `OPENALGO_API_KEY`, `HOST_SERVER`, `WEBSOCKET_URL` from environment
- Every response is checked: `if response.get("status") == "success":`
- **Mode (live vs analyze) is controlled in the OpenAlgo UI**, never from Python

## Quick start

1. Edit `pov_engine/input/pov_manual.json` to your symbol/quantity/etc.
2. Make sure OpenAlgo is running and you are in the desired mode (live/analyze).
3. Run:
   ```bash
   python -m pov_engine.runner
   ```
4. Add a new config without touching code:
   ```bash
   copy pov_engine\input\pov_manual.json pov_engine\input\my_run.json
   # edit my_run.json
   python -m pov_engine.runner pov_engine/input/my_run.json
   ```

## Adding a new signal strategy (e.g., engulfing candle)

1. Copy `pov_engine/signals/_template.py` to `pov_engine/signals/engulfing.py`.
2. Rename the class to `EngulfingSignal`.
3. Implement `detect()` using `self.client.history(...)` for candles.
4. Reference it from a JSON:
   ```json
   { "signal": "engulfing", "slicer": "pov", ... }
   ```
5. The auto-registry in `pov_engine/signals/__init__.py` picks it up — no other code change.

The POV slicer is reused. Engulfing becomes the *signal* (when to trade),
POV remains the *executor* (how to slice).

## Adding a new slicer (e.g., TWAP, Iceberg)

1. Create `pov_engine/slicers/twap.py` with class `TWAPSlicer(BaseSlicer)`.
2. Implement `execute(intent)`.
3. Reference it from JSON: `{"signal": "manual", "slicer": "twap", ...}`.
4. Auto-registered.

## Execution guards (depth-aware safety)

The POV slicer ships with three composable execution-time guards, all
implemented on top of the OpenAlgo SDK (`client.depth(...)` and
`client.quotes(...)`) and configurable from the `pov` block of any
master JSON:

* `DepthPricingGuard` — best-bid / best-ask aware LIMIT pricing, never
  crosses the spread.
* `DepthSliceCapGuard` — caps the calculated slice to the displayed
  quantity on the best opposite-side tier.
* `CircuitGuard` — auto-pauses the loop when the LTP moves within
  `circuit_buffer_pct` (default `0.5%`) of either exchange circuit limit.

See `pov_engine/input/README.md` ("Depth-aware execution guards") for
the field reference. When `client.depth(...)` is unavailable, every
guard degrades safely to the legacy `ceiling_price` / `max_slice_quantity`
paths — runs never stall.

Add new guards by subclassing `BaseGuard` in `pov_engine/slicers/guards.py`
and registering them in `default_guards()`. No slicer edit is needed.

## Logging

Every component uses a `self.log(message)` method that prints a prefixed
line, e.g. `[slicer:POVSlicer] child #3: BUY 50 @ LIMIT 1200.00 -> orderid=...`.
Output goes to stdout. Capture or redirect it the way you would any
Python script.

## What this project does NOT do

- It does **not** modify the OpenAlgo SDK or the OpenAlgo source tree.
- It does **not** toggle the analyzer mode.
- It does **not** add any new REST endpoints.
- It is pure user-side Python that talks to OpenAlgo over the existing REST API.
