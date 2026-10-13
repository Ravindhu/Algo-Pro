# blue_engine

A top-down macro trading framework built on the [OpenAlgo Python SDK](https://pypi.org/project/openalgo/) (`openalgo==2.0.3`).

## Architecture

Every sub-folder under `Blue_engine/` is an **engine** (plus `core/`, the shared kernel):

```
blue_engine/
  alpha/            Alpha engine -- signal generation (WHEN to trade)
    alpha_engine.py   AlphaEngine (fundamental + technical filters)
    signals/          Signal strategies (keylevel, ocl_keylevel, _template ...)
      base.py           Abstract BaseSignal
      keylevel.py       Manual price-level trigger
      ocl_keylevel.py   Open-Close-Low 3-candle pattern signal
      _template.py      Skeleton for your own signals
  pov/              POV engine -- execution (HOW to slice orders)
    pov_engine.py     PovEngine (execution-cost / TCA tracking)
    slicers/          Execution algorithms
      base.py           Abstract BaseSlicer
      pov.py            Percentage-of-Volume slicer
      liquidity_pov.py  Liquidity-aware POV variant
      guards.py         Depth / circuit / auction execution guards
  economics/        Economic engine (regime, GDP, bond, inflation ...)
  market/           Market engine (returns, FII flow, commodities)
  screener/         Fundamental universe filter + EPV/CANAV valuation
  backtest/         Deterministic historical simulation (research only)
  optimizer/        Bounded walk-forward parameter search (research only)
  portfolio/        Portfolio engine (construction, risk-parity, hedge)
  infrastructure/   Infrastructure engine (OpenAlgo, brokers, servers)
  core/             Shared kernel (SDK client, config, pipeline, runner, utils)
    risk_engine.py    Deterministic emergency-veto stage before POV
  dashboard/        Dash/Plotly operating console (pages/...)
  input/            User-editable JSON configs (no Python needed to edit)
```

Root contains **only** engine folders and project hygiene files (`pyproject.toml`,
`README.md`, `Dockerfile`, `__init__.py`, `_sdkpath.py`, dotfiles).

## Design rules (mirroring the OpenAlgo reference)

These come from the official OpenAlgo example `strategies/Custom Statergy/simple_ema_strategy.py`:

- `from openalgo import api`
- `client = api(api_key=..., host=..., ws_url=...)`
- All SDK method calls use **keyword-only** arguments
- Read `OPENALGO_API_KEY`, `HOST_SERVER`, `WEBSOCKET_URL` from environment
- Every response is checked: `if response.get("status") == "success":`
- **Mode (live vs analyze) is controlled in the OpenAlgo UI**, never from Python

## Quick start

1. Edit `blue_engine/input/master.json` to your symbol/quantity/etc.
2. Make sure OpenAlgo is running and you are in the desired mode (live/analyze).
3. Run:
   ```bash
   python -m blue_engine.core.runner
   ```
4. Add a new config without touching code:
   ```bash
   copy blue_engine\input\master.json blue_engine\input\my_run.json
   # edit my_run.json
   python -m blue_engine.core.runner blue_engine/input/my_run.json
   ```

## Historical backtesting and per-symbol optimization

`blue_engine.backtest` is the deterministic validation engine. It consumes
historical OHLCV only, evaluates a strategy on the visible bar prefix, and
executes each resulting target exposure at the **next bar's open**. The default
cost model includes the existing Indian statutory charge model, slippage,
latency, and volume-sensitive impact. The default position limit is long-only
and at most 100% of equity; short exposure must be explicitly enabled.

The reusable symbol-batch flow is:

1. Pass the screened ticker list, timeframe, and date window.
2. Check local SQLite cache coverage and download missing outer date windows
   in bounded chunks before any optimization begins. Downloads for different
   symbols run in a bounded thread pool; bars are not fetched inside a trial.
3. Load each symbol's cached bars in an isolated optimization worker. TPE
   searches the In-Sample (IS) partition using the configured walk-forward
   folds.
4. Re-run the recommendation on the untouched Out-of-Sample (OOS) partition.
   A symbol is included in `configurations` only when OOS Sharpe is at least
   70% of IS Sharpe and OOS maximum drawdown is no more than 130% of IS
   drawdown. Failed or insufficient runs remain visible in `results`.
5. Atomically write the approved-only lookup and per-symbol validation report
   to `data/optimizer/symbol_parameters.json` (override with `output_path`).

The signal contract is `signal(params, context, bars) -> target_position`, where
`target_position` is a finite exposure fraction in `[-1, 1]` and `bars` is a
read-only prefix. A signal may instead return
`{"target_position": fraction}`. Historical results are not live performance,
and this workflow **does not** alter live configuration, promote parameters,
or place orders. Review and explicitly promote approved configurations through
the existing risk-controlled live operating path.
`BatchConfig.risk_free_per_period` is applied consistently to optimizer Sharpe,
the cost-adjusted objective gate, and IS/OOS reporting; its default of zero is
an explicit assumption, not an India risk-free-rate estimate.

For multiple CPU workers, provide a module-level callable or import path such
as `"my_strategy:signal"` and invoke the batch from a Windows
`if __name__ == "__main__":` guard. The default annualization convention is
252 daily periods, 52 weekly periods, 12 monthly periods, and 375 trading
minutes per intraday session; set `periods_per_year` explicitly when that
assumption does not match the data.

```python
from blue_engine.backtest.batch import BatchConfig, run_symbol_batch
from blue_engine.optimizer.spaces import load_space

if __name__ == "__main__":
    screened_symbols = ["RELIANCE", "INFY", "TCS"]
    result = run_symbol_batch(
        screened_symbols,
        strategy="my_strategy:signal",
        search_space=load_space("ocl_keylevel"),
        config=BatchConfig(
            start="2023-01-01", end="2025-12-31", interval="1d",
            trials=100, folds=4, oos_fraction=0.2),
    )
    print(result.output_path)
```

## Front-line decision engines

The economic and market engines retain their existing quantitative outputs and
add bounded decision fields for downstream consumers. Economic regime labels
use configured GDP/inflation cut-offs; the market engine estimates annualized
realized volatility only when a volatility figure or daily percentage returns
are supplied. Its 15%/30% volatility cut-offs are configurable working
assumptions, not market forecasts. Missing volatility is reported as
`Elevated`, never as verified low risk.

`MacroPipeline` always runs the risk stage before POV. Screener and valuation
stages are opt-in via `engines.screener.pipeline_enabled` and
`engines.valuation.pipeline_enabled`; the screener's network access is
therefore never enabled just by constructing the default pipeline.

`PortfolioEngine` enforces the configured minimum cash reserve and caps
technology-sector gross exposure when `sector_by_symbol` is supplied. Missing
sector classifications are surfaced as a warning, not guessed. A risk veto or
high-panic market state suppresses proposed rebalance trades.

`ScreenerEngine` accepts `legal_warnings` alongside its candidate snapshot.
Entries are applied as a blacklist only when each includes a ticker, reason,
source, and the explicit boolean `verified: true`, which is an upstream
attestation by the data provider (not an independent source verification by
this engine); unverified text is reported but cannot silently exclude a
security. `ValuationEngine` uses the existing
EPV/CANAV implementation on caller-supplied company snapshots. Its default
growth ceiling (5%) and margin-of-safety adjustment (0.85) are configurable
policy bounds, not model-estimated growth or a promise of fair value.

`RiskEngine` converts explicit latency, data-integrity, conflict, slippage, and
panic inputs into a veto. A failed live risk assessment blocks the macro
pipeline's POV stage. The live runner's independent pre-trade `RiskGate`
remains mandatory; neither risk output nor a text model can replace verified
broker, position, or market-data checks. The engine does not write
configuration files or place orders.

The deterministic backtest and TPE optimizer remain offline research engines:
they do not promote parameters or change live configuration. Their existing
walk-forward, cost, and out-of-sample checks remain the validation path.

### Persistence, messaging, and concurrency

The implemented research path uses the existing SQLite OHLCV cache, an
atomically replaced JSON recommendation/report, `ThreadPoolExecutor` for
bounded downloads, and Windows-safe `ProcessPoolExecutor` workers for
symbol-isolated parameter searches. It adds no service or package dependency.
The live monitor is wired into the POV child-order boundary when explicitly
enabled in the raw runner config. It requires a trusted live telemetry provider
and historical envelope; each attempted child order verifies the broker
positionbook and fresh telemetry. Monitor threshold breaches pause new entries,
allow verified exit-phase orders, and latch the durable kill switch. Missing or
invalid broker positions or telemetry also latch the switch and abort execution.
A quote-feed outage reaching the POV pause threshold also latches the switch.
A latched switch blocks subsequent runs until an operator explicitly resets it.
A latched switch blocks subsequent runs until an operator explicitly resets it.
The runner does not automatically cancel already-working broker orders or
promote parameters.

Example opt-in runner configuration:

```json
{
  "live_monitor": {
    "enabled": true,
    "provider_ref": "my_trading.telemetry:read_monitor_inputs",
    "historical_envelope": {
      "max_dd": 8.0,
      "max_losing_streak": 6
    },
    "check_interval_seconds": 30,
    "regime_window": 10
  }
}
```

The provider is called before each child order as
`provider(client=..., positionbook=..., monitor_config=...)`. It must return
finite, current `live_equity`, `live_trade_pnls`, `live_daily_returns`, and
`historical_daily_returns` sequences; the historical returns need at least
`regime_window + 1` observations. `max_dd` is in percentage points, matching
the monitor's live drawdown calculation. The provider must source these values
from verified account/trade/market data, not static config or backtest output.
On valid quote polls, telemetry is refreshed no more often than
`check_interval_seconds` (default 30); every child order forces a fresh
broker-position and telemetry verification regardless of this interval.
Durable kill-switch storage must be available. The monitor defaults to
disabled; enabling it without a valid provider blocks the run rather than
silently skipping checks.

For a later multi-process production deployment, keep PostgreSQL as the
authoritative store for state transitions, run manifests, approved parameter
versions, fills and audit history. Use a durable RabbitMQ queue for
re-optimization/reconciliation commands and a dead-letter queue for failed
work; workers should be idempotent and only report completion after durable
state is committed. The current local process pool is appropriate for research
on one Windows host; introduce a distributed worker framework such as Ray only
when profiling demonstrates a need to scale beyond that host. SQLite, JSON,
and local process workers are not a substitute for these shared production
services.

## Adding a new signal strategy (e.g., engulfing candle)

1. Copy `blue_engine/alpha/signals/_template.py` to `blue_engine/alpha/signals/engulfing.py`.
2. Rename the class to `EngulfingSignal`.
3. Implement `detect()` using `self.client.history(...)` for candles.
4. Reference it from a JSON:
   ```json
   { "signal": "engulfing", "slicer": "pov", ... }
   ```
5. The auto-registry in `blue_engine/alpha/signals/__init__.py` picks it up — no other code change.

The POV slicer is reused. Engulfing becomes the *signal* (when to trade),
POV remains the *executor* (how to slice).

## Adding a new slicer (e.g., TWAP, Iceberg)

1. Create `blue_engine/pov/slicers/twap.py` with class `TWAPSlicer(BaseSlicer)`.
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

See `blue_engine/input/README.md` ("Depth-aware execution guards") for
the field reference. When `client.depth(...)` is unavailable, every
guard degrades safely to the legacy `ceiling_price` / `max_slice_quantity`
paths — runs never stall.

Add new guards by subclassing `BaseGuard` in `blue_engine/pov/slicers/guards.py`
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
