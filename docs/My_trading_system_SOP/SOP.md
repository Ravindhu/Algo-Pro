# My Trading System — Standard Operating Procedure

**Last updated:** 2026-10-29 (rev 2 - supervisor)
**Applies to:** `C:\Users\Admin\CodeGPT\my-trading-system\Blue_engine`
**Companion document:** `docs/audits/Economic_to_POV_Pipeline_Audit_2026-10-22.md`

> **Path note (resolved 2026-10-23).** `docs\My trading system SOP` used to be a
> 30 KB **file**, which shadowed the directory path and made every SOP write fail.
> It was retired to `docs\My_trading_system_SOP\SOP_2026-10-14.md` (superseded
> reference copy), so `docs\My_trading_system_SOP\SOP.md` is now the single
> writable SOP that this document is updated in place.

---

## 1. Pre-session checklist

Run every item before an unattended session. Each is ≤ 1 minute.

```powershell
cd C:\Users\Admin\CodeGPT\my-trading-system
```

| # | Check | Command | Pass looks like |
|---|---|---|---|
| 1 | OpenAlgo up | `Test-NetConnection 127.0.0.1 -Port 5000 -InformationLevel Quiet` | `True` |
| 2 | Kill switch clear | `.venv\Scripts\python.exe -c "import sys;sys.path.insert(0,'Blue_engine');from blue_engine.portfolio.optim_policy import peek_kill_switch;print(peek_kill_switch())"` | `'tripped': False` |
| 3 | Macro sources alive | `.venv\Scripts\python.exe Temp\Audit\snapshot_provenance.py` | ≥ 3 live, `ok=True` |
| 4 | Tests green | `.venv\Scripts\python.exe -m pytest Blue_engine/tests/test_economic_to_pov_audit.py` | `15 passed` |
| 5 | OpenAlgo mode | check `[runner] OpenAlgo mode =` on start | `analyze` for paper |
| 6 | Risk caps set | `.venv\Scripts\python.exe Temp\Staging Workspace\verify_risk_wiring.py` | `100 INFY @1480 live approved: True` and every `_MONEY_CAPS` row `active=True` |
| 6b | Broker session valid | supervisor gate `broker_session`, or check `authenticated` in the journal | live login at `/broker`; OpenAlgo expires the session daily |
| 6c | Inside the session | supervisor gate `market_hours` `ok` | weekdays 09:15–15:30 IST, entries only until `14:45` |
| 6d | **Paper trade (no broker needed)** | `.venv\Scripts\python.exe -m blue_engine.core.runner Blue_engine/input/master.json --paper` | full path runs; report ends `===== PAPER SESSION REPORT =====` |
| 7a | **Start under the supervisor** | `.venv\Scripts\python.exe scripts\run\run_supervised.py` | gates pass, engine starts; a blocked gate exits `4` and writes `data/supervisor_journal.jsonl` |
| 7 | Exit engine configured | `python -c "import json;print('exit_engine' in json.load(open('Blue_engine/input/master.json')))"` | `True` — a filled position is otherwise **unprotected** |

> **Exit engine (2026-10-29).** `core/exit_engine.py` places a broker **SL-M**
> for every open position after the slicer finishes, verifies it on the
> orderbook, reconciles every `check_interval_seconds` for `supervise_seconds`,
> and cancels orphaned stops. A **CRITICAL** report (unreadable positionbook,
> failed placement, marketable stop, no level) trips the durable kill switch —
> the run then exits `4` and no later run can add to an unprotected book.
> It refuses to guess: market flattening needs `allow_market_exit`, and a stop
> is never placed on the wrong side of the market. The screener bridge needs
> `--exit-config`; without it every fill logs **UNPROTECTED** and the run exits `4`.

> **Supervisor (2026-10-29).** `core/supervisor.py` + `scripts/run/run_supervised.py`
> run the engine as a supervised child. Before **every** start it passes six
> gates: kill switch clear, OpenAlgo reachable, positionbook verifiable, no
> unreconciled working orders for `POV-EXIT`, a sane quote, and (by rule) no
> restart after a safety exit. Exit `4` is **never** retried - a human reviews.
> Exit `5` (order feed down) is retried with exponential backoff, capped at
> `max_restarts` per `restart_window_seconds`. Every event goes to
> `data/supervisor_journal.jsonl` and the `/risk` page shows the state; an
> absent journal reads **unknown**, never "running". `start_all.py --engine`
> starts it (off by default; the platform alone is the safe default).
>
> **Paper trade (2026-10-29, rev 4).** `--paper` swaps the broker for
> `core/paper_broker.PaperBroker`: the FULL path (signal -> risk gate -> POV
> -> ledger -> exit engine) runs, but every order is recorded, never sent.
> Real quotes are used when a login exists; otherwise a **deterministic
> synthetic tape** is used and the report says `synthetic`. Paper is exempt
> from the session gate and from the live fill-feed check (fills are
> synchronous). It proves the PLUMBING, never strategy performance.
>
> **Auto-tune (2026-10-29).** `core/auto_tune.AutoTuner` closes the feedback
> loop: a triggered `ReoptimizationSignal` now re-runs the batch optimizer and
> promotes approved parameters. See `auto_tune.mode`:
> `shadow` (default) computes and reports only; `apply` writes the config after
> a quarantine, a type check against the existing keys, and an atomic swap.
> It never un-halts trading - re-arming stays an operator action.
>
> **Session + key (2026-10-29, rev 3).** Two blockers fixed here:
> 1. Nothing loaded the repository-root `.env`, so `OPENALGO_API_KEY` resolved
>    to None and the client was built unauthenticated — the run died at its
>    first broker call. `core/client.py` now loads `.env` (process env still
>    wins) and `init_client()` **raises at construction** when no key exists.
> 2. There was no weekday/session guard. The runner now refuses NEW exposure
>    outside 09:15–15:30 IST (exit code `6`) and the supervisor parks with a
>    `market_hours` gate; an `entry_cutoff` (default 14:45) stops new entries
>    before the close. Paper/analyze runs are exempt. Exchange holidays go in
>    `Blue_engine/input/market_holidays.json` (absent = weekday check only, and
>    the reason string says so rather than implying a verified holiday).

> **Risk block (2026-10-29).** `Blue_engine/input/master.json` now carries a
> `risk` block: `max_order_quantity`, `max_order_notional`, `max_daily_loss`,
> `max_open_exposure`, `require_live_ack: true`, `blocked`. A **missing key is
> not a default** — it means that cap is not enforced, and `require_live_ack`
> then rejects the whole live run and names the missing cap. `day_pnl` and
> `open_exposure` come from the live broker `positionbook`
> (`core/risk_context.py`) unless set explicitly in the block. The `/risk`
> dashboard page shows each cap as ACTIVE/OFF and never writes a limit.

> **Read the macro table, not just the `ok`.** `stale=False` is currently
> unreliable for monthly series (audit F-08). Check `employment`'s `as_of` by eye.

---

## 2. The two entry points — know which one you are using

This is the most important operational fact in the system.

| | `MacroPipeline` | `runner.run_with_config` |
|---|---|---|
| Entry | library (`blue_engine.core.pipeline`) | `scripts/run/run_pov.py` |
| Stages | Economic → Market → Portfolio → Alpha → Risk → POV | signal → RiskGate → POV |
| Uses economic regime | **yes** | **no** |
| Live/paper control | `dry_run` flag (not enforced — audit F-17) | OpenAlgo `analyze` mode |
| Blocks for | `duration` (default 900 s) | `duration` |
| Intended use | batch / research / rebalance proposal | single execution run |

**They are not interchangeable.** `run_pov.py` does **not** consult the Economic
engine. A RISK_OFF macro state has **no effect** on a `run_pov.py` session.

---

## 2a. Starting the stack (one launcher)

Everything local is started by **one** command. Nothing needs a terminal window
left open — the services run detached and keep running after you close it.

```powershell
# Start everything
.\.venv\Scripts\python.exe scripts\ops\start_all.py

# ...or double-click install.bat (installs, updates and starts)
```

| Port | Service | Needed for |
|---|---|---|
| 5000 | OpenAlgo (Flask/SocketIO) | every order, broker session |
| 8765 | WebSocket proxy | live quotes |
| 8050 | Dash terminal | dashboard |

Other commands:

| Command | Does |
|---|---|
| `start_all.py --check` | prerequisites + ports only, then exit |
| `start_all.py --force` | kill port holders without asking |
| `start_all.py --no-docker` | force the native path |
| `start_all.py --update` | pip + OpenAlgo pull + data (**off by default**) |
| `start_all.py --stop` | stop everything |

### Facts learned the hard way

1. **OpenAlgo starts the WebSocket proxy itself.** Its boot fails outright if
   8765 is already taken (`Cannot start WebSocket server with port switching`).
   Never launch `python -m websocket_proxy.server` separately.
2. **OpenAlgo takes ~200 s cold**, not 3–5 s. It bridges `_restored/`, runs the
   upgrade check, loads the master contract and starts the strategy module. Give
   the health check 300 s; a shorter timeout reports a healthy server as failed.
3. **Dash takes ~40–60 s** to import the engine graph.
4. **Updates are off by default.** `pip install -U` and `git pull` inside
   `openalgo/` can break a working desk. Opt in with `--update`.
5. **The launcher never logs into the broker.** If the restored session is
   stale, log in yourself at `http://127.0.0.1:5000/broker`.
6. Windows has no gunicorn (`fcntl` is Unix-only), so the native path runs the
   Werkzeug dev server (`start_openalgo.py`, `allow_unsafe_werkzeug=True`).
   **That is a loopback-only dev server, not a production one.**

Logs: `Temp/run/<service>.log`. PIDs: `Temp/run/start_all.pids.json`.

---

## 3. Running an execution (the live path)

### 3.1 Edit `scripts/run/run_pov.py`

```python
USER_CONFIG = {
    "strategy_label": "POV-INFY-NSE",   # unique per run; see 3.4
    "symbol":   "INFY",
    "exchange": "NSE",
    "strategies": {"keylevel": False, "ocl_keylevel": False, "pov": True},
    "pov": {
        "action": "BUY",
        "quantity": 1000,
        "participation_pct": 5.0,
        "duration_seconds": 900,
        "poll_interval_seconds": 15,
        "max_slice_quantity": 100,
        "ceiling_price": 1520.0,        # hard halt above this
        "cutoff_time":    "15:15",
        "squareoff_time": "15:20",
    },
}
```

### 3.2 Pre-flight values you must set

| Field | Why it matters | Safe default |
|---|---|---|
| `cutoff_time` | stops **new** entry slicing | `15:15` |
| `squareoff_time` | exits the position (see §6) | `15:20` |
| `ceiling_price` | hard halt; checked every poll | vwap + 2 % |
| `max_slice_quantity` | caps child size vs displayed depth | 100 |
| `poll_interval_seconds` | ≥ 3 s; every child triggers a broker round-trip | 15 |

### 3.3 Start

```powershell
$env:OPENALGO_API_KEY = "<key>"
.venv\Scripts\python.exe scripts\run\run_pov.py
```

Watch for this line before any order:

```
[runner] order-update WebSocket subscription: active
[runner] volume-cache WebSocket: active
```

If either says `degraded`, **stop**. The slicer slices on volume delta; a degraded
volume feed yields `volume=0` and therefore **no executable size**.

### 3.4 Strategy label rules (audit F-11)

`strategy_label` is the cancellation scope. Before the fix, `"MACRO-POV"` also
matched `"MACRO"` and cancelled sibling strategies' working orders.

- Use a **unique** label per run: `POV-INFY-NSE-<date>`.
- Never share a label across concurrent runs.
- If a legacy tag must be treated as yours, list it explicitly:
  `"allowed_strategy_labels": ["MACRO"]` — never rely on prefix matching.

---

## 4. Reading the output

### 4.1 Exit codes

| Code | Meaning | Action |
|---|---|---|
| 0 | completed (may still be under-filled) | reconcile §5 |
| 2 | config invalid | fix config |
| 3 | could not build slicer/signal | check strategy name + params |
| 4 | **blocked** — kill switch, risk gate, monitor | **do not retry blindly**; read the log |

### 4.2 Slicer summary fields

```python
{"filled": 0, "target": 1000, "child_orders": 0,
 "mode": "analyze", "phase": "accumulating",
 "execution_id": "POV-INFY-NSE-ab12cd34",
 "ceiling_halts": 0, "children": [...], "reason": "..."}
```

| Field | Reading it correctly |
|---|---|
| `phase` | `accumulating` → entering · `exiting` → closing · `aborted` → **never traded** |
| `reason` | present only for abnormal termination (audit F-10) |
| `ceiling_halts` | > 0 means price ran away from the band anchor |
| `filled` | **submitted-and-acknowledged**, not necessarily filled — see §5 |

> **Trap:** before the audit, a run that aborted on a data fault returned
> `phase="accumulating", filled=0` — indistinguishable from "the market gave me no
> volume". Always check `reason`.

---

## 5. Reconciliation (mandatory, after every run)

`filled` is reconciled from the **durable ledger** (`recover_filled_quantity`), not
from the in-memory counter. Confirm against the broker.

```powershell
# 1. Broker truth
#    OpenAlgo UI -> Orders -> filter by strategy_label

# 2. Ledger truth
.venv\Scripts\python.exe -c "import sys;sys.path.insert(0,'Blue_engine');from blue_engine.core import ledger;print(ledger.summarise('POV-INFY-NSE-<id>'))"
```

**They must agree.** If the broker shows more fills than the ledger, the ledger
reconciliation failed silently (audit F-20) — a known gap. Stop and investigate
before the next run.

### 5.1 Position check

```powershell
.venv\Scripts\python.exe -c "import sys;sys.path.insert(0,'Blue_engine');from blue_engine.core.client import init_client;c=init_client();print(c.positionbook())"
```

---

## 6. Square-off and cutoff behaviour (audit F-13)

| Time | What happens |
|---|---|
| `cutoff_time` | phase → `exiting`; the slicer **reverses side** and closes what it filled |
| `squareoff_time` | same, at a higher exit participation (`exit_participation_pct`, default 30 %) |
| Launch **after** either time | `phase="aborted"`, `reason="..._reached_at_start"` — no orders |

> **Verified safe behaviour:** the exit target is pinned to what the entry
> **actually filled**, so a partially filled entry cannot open an unhedged short.

**What is NOT automatic:** a launched run that ends with `phase="accumulating"`
because `duration` expired leaves the position **open**. Square-off is a separate
run. Do not assume it happened.

---

## 7. Kill switch

### 7.1 What it blocks

| Layer | Checked |
|---|---|
| `runner` start | once |
| `runner` pre-execution | re-read after the signal wait (audit F-13) |
| slicer, **every child** | `kill_tripped` hook (audit F-13) |
| `optim_policy.decide()` | before any optimise |

### 7.2 Trip it

```powershell
.venv\Scripts\python.exe -c "import sys;sys.path.insert(0,'Blue_engine');from blue_engine.portfolio.optim_policy import trip_kill_switch;print(trip_kill_switch('operator halt: <reason>'))"
```

### 7.3 Reset it — requires a token

```powershell
.venv\Scripts\python.exe -c "import sys;sys.path.insert(0,'Blue_engine');from blue_engine.portfolio.optim_policy import reset_kill_switch;print(reset_kill_switch(confirm='operator-name'))"
```

The switch is **persisted and latching**. A restart comes up halted; that is by
design. Never reset without knowing why it tripped.

---

## 8. Economic regime → position sizing

### 8.1 How the tilt works (fixed 2026-10-22)

```
risk_on_score  ->  multiplier = clamp(1 + score, 0.25, 1.5)
               ->  Portfolio scales target weights
               ->  hard caps re-applied (gross <= 0.85)
               ->  POV trims the execution quantity by the same multiplier
```

| Regime | Score | Multiplier | Book effect |
|---|---|---|---|
| RISK_ON | >= 0.35 | up to 1.50 | larger, clipped at gross 0.85 |
| NEUTRAL | -0.10...0.10 | ~1.00 | unchanged |
| RISK_OFF | <= -0.35 | as low as 0.25 | sharply smaller |

**Check it worked:** the report carries `stages.portfolio.data.economic_tilt`.

```json
{"applied": true, "multiplier": 0.74, "pre_tilt_gross": 0.85,
 "post_tilt_gross": 0.63, "regime": "MILD_RISK_OFF"}
```

If `applied` is `false`, read the `reason` — the book was **not** de-risked.

### 8.2 Signal trust (live check 2026-10-22)

| Signal | Source | Trust |
|---|---|---|
| inflation | MoSPI, live | high (monthly) |
| gdp_growth | MoSPI, live | high (quarterly) |
| currency | RBI FBIL, live | high (daily) |
| employment | MoSPI, live | **check `as_of`** — was 10 months old |
| FII / DII cash | **vendor parquet** | **check `stale`** — lagging since 2026-10-13 |
| India VIX | **vendor parquet** | current to 2026-10-23 (refreshed by `vendor_maintenance`) |
| bond_return | static | **none** — does not update |
| debt | static | **none** |
| forex_reserves | static | **none** |

Still ~33 % of the composite weight is static. Treat the regime label as
directional, not precise.

### 8.3 FII / DII — now read from the vendor checkout (no scraping)

Changed 2026-10-22. `FiiFlowProvider` reads, in order:

1. `<repo>/Blue_engine/database/vendor/indian-market-historical-data/data/FLOWS/`
   - `fii_dii_cash.parquet` → FII net cash, DII net cash (₹ Cr)
   - `fii_derivatives.parquet` → FII index-futures long ratio, net OI
   - `data/INDICES/INDIA_VIX.parquet` → VIX
2. NSE JSON — fallback only, when the vendor file cannot supply the cash flow

Measured on this host: **8.9 s → 0.07 s** for the flow read, and the full macro
stack went **19 s → 3.1 s**.

**Freshness is checked, not assumed.** The newest vendor row is compared against
today in *trading* days (`max_lag_trading_days`, default 2). Beyond that the
values are still served but `meta.stale = True` and a warning is emitted.

> **Current state (2026-10-24): the INDICES pin is current, the FLOWS pin is not.**
> The indices were refreshed to **2026-10-23** by
> `blue_engine.jobs.vendor_maintenance`. FII/DII rows (`data/FLOWS/`) are still
> at **2026-10-13** - that upstream repo carries no newer flow data, so expect
> `vendor flows STALE` until it does.

### 8.4 Refreshing the vendor data (routine, not manual)

The index data is refreshed by a tested job, not by hand-editing files:

```powershell
cd Blue_engine
..\.venv\Scripts\python.exe -m blue_engine.jobs.vendor_maintenance
```

Useful flags: `--check` (report only, writes nothing), `--backfill` (full
history — use this when adding an index), `--only NIFTY_IT,NIFTY_AUTO`,
`--max-age-days 5`, `--json`.

It does five things: refresh every registered index, **drop unusable rows**
(NaN / non-positive / non-finite closes), verify each index advanced, rebuild
`index_list.csv`, and exit **non-zero** when any index failed or the newest
session is older than `--max-age-days`.

**The index registry is `INDEX_TICKERS` in
`Blue_engine/sectors/providers/vendor_sector.py`.** That is the only list to
edit when adding an index — the job, the sector engine and
`scripts/ops/update_vendor_data.py` all read it.

**Scheduling.** Register the daily pre-market job (once, from an elevated
shell — `Register-ScheduledTask` needs it):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ops\register_vendor_data_job.ps1
```

Runs 08:45 IST daily plus at logon, logs to
`data\logs\vendor_maintenance.log`, and keeps `Last Run Result` meaningful:
non-zero means "a human should look".

> **Verified on this host (2026-10-24):** the task `BlueEngine_VendorData` is
> **registered and working** - daily 08:45 IST plus at logon, `LastTaskResult: 0`
> on a manual run, 24 indices refreshed, next run the following morning.
> Log: `data\logs\vendor_maintenance.log`. Registration needs an elevated
> shell (`Register-ScheduledTask` returns `Access is denied` otherwise).

**Known data caveats in this checkout:**

| File | Caveat |
|---|---|
| `MACRO/cpi_inflation.parquet` | last print **2025-03** — use the live MoSPI provider, not this file |
| `INDICES/*.parquet` | newest bar is **sometimes partial** (Close = NaN, Volume = 0). The maintenance job now DROPS these — one such row on 2026-10-13 broke the sector alignment for every index |
| `FLOWS/*.parquet` | lagging; always read `meta.stale` |

---

## 8b. Sector rotation (HM score) — RESEARCH VERDICT: do not enable

Changed 2026-10-24. **Read this before using the sector HM score for capital.**

The sector sleeve idea was: score each NSE sectoral index with the
Hilega-Milega reading (RSI(9) vs EMA(3)), rank them, and allocate to the most
discounted. The machinery exists and works:

```powershell
cd Blue_engine
..\.venv\Scripts\python.exe -m blue_engine.jobs.sector_hm_report --top 5
..\.venv\Scripts\python.exe -m blue_engine.jobs.sector_dataset_report
```

`sector_hm_scores.json` is the daily table; `sector_dataset.json` is the
research dataset (53,130 rows, 3,830 dates, 2007→2026, 14 sector indices,
5d/20d forward-return labels, verified zero look-ahead by truncation test).

**The verdict is negative.** On out-of-sample walk-forward folds:

| Test | 5-day | 20-day |
|---|---|---|
| Buy MOST-discounted sector → top tercile | 48.3% | **44.4%** |
| Random | 50.0% | 50.0% |
| Rank correlation (HM vs forward label) | **−0.047** | **−0.043** |

The signal is **inverted**: on this data, "most discounted" is a negative
predictor of relative strength, consistently at both horizons. Buying the
least-discounted (i.e. momentum) did better. A GBM ranker only reached a
positive Sharpe by learning that inversion (4.21 vs −0.005 at 20d).

Consequences, in force until new evidence changes them:

1. `engines.sector.pipeline_enabled` stays **false** in `macro_master.json`.
2. Nobody re-implements "allocate to the most discounted sector" without
   reading this section.
3. The HM report remains useful as **telemetry** — it correctly returned
   "0 sectors qualify" on 2026-10-24 rather than recommending five falling
   sectors.

If this is revisited, the direction to test is **momentum within sectors**
(buy relative strength), which must be walk-forwarded on the same dataset and
beat the same baseline. `blue_engine/ml/evaluation/sector_ranker_eval.py`
provides the comparison harness.

### The momentum direction was tested. It is real but NOT tradeable.

Measured 2026-10-24 on the same dataset (3,810 dates, 5-day horizon):

| Time block | Strongest sector | Weakest sector |
|---|---|---|
| 2011–2015 | 0.542 | 0.445 |
| 2015–2018 | 0.496 | 0.454 |
| 2018–2022 | 0.528 | 0.454 |
| 2022–2026 | 0.547 | 0.503 |
| **mean** | **0.528 (+2.8 pp)** | **0.465 (−4.0 pp)** |

The direction is consistent in 3 of 4 blocks (2015–2018 is a flat 0.496) at
both 5d and 20d. So the ordering is real, not noise: relative strength
persists and relative weakness persists.

**But it does not survive costs.** A 2.8 pp hit-rate edge on a 100–200 bps
cross-sector move is ~3–6 bps of gross edge per period, against a 10 bps
round-trip:

| Cross-sector dispersion | Gross edge | Net after 10 bps |
|---|---|---|
| 100 bps | ~2.8 bps | **−7.2 bps** |
| 150 bps | ~4.2 bps | **−5.8 bps** |
| 200 bps | ~5.6 bps | **−4.3 bps** |

Negative in every case. **Sector-level rotation on these ~14 indices is too
coarse to pay for a delivery round trip.** It would need either far lower
costs, direct/index derivatives, or a longer holding period with a much larger
edge per trade.

Practical conclusion: **do not build sector rotation into the live path.** The
research harness stays in place so the conclusion can be re-tested if costs or
the universe change.

Two related caveats that limit any sector-level research here:

- **7 NSE sectoral indices have no usable price source** (CEMENT, POWER,
  CAPITAL GOODS, OIL & GAS, HEALTHCARE, CHEMICALS, PVT BANK — probed 17 symbol
  variants, all empty). Any universe is ~14 of ~21 sectors, which is a thin
  cross-section (terciles of 4–5 names).
- **Survivorship**: the universe is today's parquets;
  `first_date_per_sector` in the dataset provenance shows each sector's real
  start (MIDCAP 100 only begins 2022).

---

## 8c. OCL blueprint comparison — gate before ANY paper enable

Added 2026-10-28. **No `setup_mode` goes to paper trading without this run.**

The OCL keylevel strategy ships three BUY blueprints (`setup_mode`): `OCL`,
`SELL_FAILURE_TOP`, `SELL_FAILURE_BOTTOM`. They are three different trading
ideas sharing one code path, so "which one" is a research question, not a
preference. `blue_engine/backtest/blueprints.py` answers it.

### Run it

```powershell
cd C:\Users\Admin\CodeGPT\my-trading-system

# bars must already be in the optimizer cache; this step downloads what is missing
.\.venv\Scripts\python.exe -m blue_engine.backtest.blueprints `
  --symbols INFY,TCS,RELIANCE,HDFCBANK,SBIN `
  --start 2025-01-01 --end 2026-06-30 `
  --trials 100 --folds 4 --oos-fraction 0.2 --seed 7

# same run, explicit blueprint list and report location
.\.venv\Scripts\python.exe -m blue_engine.backtest.blueprints `
  --symbols symbols.csv --start 2025-01-01 --end 2026-06-30 `
  --modes OCL,SELL_FAILURE_TOP,SELL_FAILURE_BOTTOM `
  --out data\backtest_results\blueprint_comparison.json
```

Output: `blueprint_comparison.json` + `.md` next to it (one table, one
verdict), and one candidate parameter file per blueprint under
`blueprint_candidates/`. **Nothing is enabled by this run.**

Exit codes: `0` RECOMMENDED, `2` TIE, `3` NO BLUEPRINT ELIGIBLE. Chain the
command only on `0`.

### What the run holds constant (read before quoting a number)

| Held constant | Why it matters |
|---|---|
| symbols, start, end | a blueprint is never compared on a bigger universe than its rival; a symbol that fails for one blueprint is dropped for all |
| cost model | one `CostModel` instance (statutory stack + slippage + latency + impact) for every run |
| fold geometry, seed, trials | the deflated-Sharpe correction is comparable across the table |
| every parameter except `setup_mode` | the study space is pinned to a single categorical value, so the difference is the blueprint, not a lucky `entry_z` |

Only out-of-sample numbers appear in the table. Expectancy is net of the
engine's per-fill costs; Sharpe is the median of per-symbol OOS Sharpe;
deflated probability is the WORST per-symbol value.

### The gate (all five must pass)

1. pooled OOS trades ≥ 10
2. post-cost expectancy > 0
3. median OOS Sharpe ≥ 1.0
4. worst OOS deflated probability ≥ 0.95
5. every symbol `APPROVED` by the Loop A gate

Eligible blueprints are ranked by Sharpe then expectancy. A runner-up inside
**0.25 Sharpe** produces a `TIE` and no recommendation — that gap is inside
the sampling error of a one-year Sharpe estimate.

### Enabling for paper (manual, after a RECOMMENDED verdict)

1. Confirm the verdict names the blueprint you intend to run.
2. Set `"setup_mode"` in the `ocl_keylevel` config block
   (`scripts/run/run_pov.py` / `input/*.json`) to that value. Keep
   `strategies.ocl_keylevel = false` until the paper session is scheduled.
3. Run paper (OpenAlgo `analyze` mode) and reconcile per §5.
4. Record the comparison `timestamp`, universe and window in the work log —
the verdict is only valid for the data it was measured on.

### Regression tests

```powershell
.\.venv\Scripts\python.exe -m pytest Blue_engine\tests\test_backtest_blueprints.py -q
```

---

## 9. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `POV blocked: infrastructure gate failed` | OpenAlgo/broker down | start OpenAlgo, re-run |
| `no alpha-selected symbol` | nothing passed the backtest gate | check `alpha.candidates` |
| `symbol not in target portfolio` | book has zero weight for it | review the portfolio stage |
| `no client` / `dry_run` | no SDK client injected | expected in research |
| `volume-cache WebSocket: degraded` | WS not up | **do not trade** |
| `PAUSED after N failures` | quote feed down | check broker session |
| `CEILING HALT` repeatedly | price above the band anchor | widen ceiling or wait |
| `kill switch tripped during the run` | operator/other process tripped it | investigate, then reset |
| exit code 4 | risk gate rejected | read `RISK GATE REJECTED` reason |

---

## 10. Change control for this flow

Any change touching the Economic → POV path must:

1. Keep `test_economic_to_pov_audit.py` green (15 tests).
2. Preserve the invariant: **the multiplier may only reduce exposure, never
   increase it past the configured caps.**
3. Pass the hard-cap test: `gross <= min(max_gross, 1 - min_cash_buffer_pct)` under
   **every** multiplier from 0.25 to 1.5.
4. Add a regression test for any new seam.
5. Update this SOP and the audit document in the same commit.

---

## 11. System facts that are easy to get wrong

| Fact | Consequence |
|---|---|
| `filled` = acknowledged, not filled | always reconcile (§5) |
| Portfolio sleeves are **indices**, execution is a **stock** | the budget fallback is used and **warns**; watch for it |
| Exit target = filled quantity, not parent quantity | partial fill → partial exit (correct) |
| `duration` expiry does **not** square off | run a separate exit |
| `stale=False` is unreliable for monthly series | check `as_of` by eye |
| `run_pov.py` ignores the Economic regime | the tilt does not apply to that path |
| An LLM backend cannot loosen a limit | `_CAPS` + `_escalate` in `core/frontline_agent.py` clamp the safe direction; a veto can never be cleared by the agent |

---

## 12. Frontline LLM chain (Ollama → OpenRouter → disabled)

Advisory only. Nothing in this chain places, sizes or cancels an order.

### Setup (both are optional; either alone is enough)

| Backend | Config | Notes |
|---|---|---|
| Ollama (primary) | `OLLAMA_HOST` (a **URL**, not a key) | default `http://127.0.0.1:11434`; unlimited, no quota |
| OpenRouter (overflow) | `OPENROUTER_API_KEY` | free `:free` models: 20 req/min, 50/day (<10 credits) or 1000/day |

```powershell
# Is Ollama up? (no key is involved)
Invoke-RestMethod http://127.0.0.1:11434/api/tags | Select-Object -ExpandProperty models
```

### Turn it on

`Blue_engine/input/macro_master.json` → `engines.agent.enabled = true`. The
`engines.agent.llm` block sets per-backend `enabled`, `model` and `digest`.

### Verify the chain order

```powershell
cd C:\Users\Admin\CodeGPT\my-trading-system
.\.venv\Scripts\python.exe -c "import sys;sys.path.insert(0,'Blue_engine');from blue_engine.core.frontline_agent import FrontlineAgent;print(FrontlineAgent({'enabled':True}).status()['chain'])"
```

Expect `chain: ['ollama','openrouter']` and, per backend, whether it is
`configured`. If both show unconfigured, the agent returns `method='disabled'`
and the pipeline continues with its deterministic values — that is normal.

### Rules that must not change

1. **Pin the model digest.** `:latest` is mutable; an upgrade would change agent
   output silently. Fill `llm.ollama.digest` once known.
2. **A 429 retires OpenRouter for the UTC day.** It is not retried.
3. **Agentic mode is bounded**: `max_rounds` ≤ 4, `max_calls` ≤ 6, clamped in
   code regardless of config. Tools must be **read-only**.
4. **Escalation only**: for veto/sizing keys the agent may move toward the safe
   side, never away. Tested in `tests/test_llm_chain.py`.

---
## 12. Agent Line — the agentic frontline (2026-10-26)

The stage skills are a **factory line**: the product is one risk-checked trading
decision, each stage is a station, and the deterministic engine at each station
is the machine. The employee supplies raw material and reports what is missing;
it never operates the machine.

### 12.1 What the agent may and may not do

| May | May not |
|---|---|
| Answer inside its stage schema | Write code, config or order state |
| Veto for any reason (risk station) | Clear a veto or widen a limit |
| Blacklist any symbol with a reason | Add a symbol to the universe |
| **Ask for missing evidence** (`data_requests`) | Invent a number when evidence is absent |
| Read JSON + call wired read-only providers | Touch the broker or the POV slicer |

Enforcement lives in code, not in prompts:

* `core/skill_writer.py` — the only write path; refuses non-`.json`, path
  traversal, NaN/inf, and every protected config basename.
* `core/frontline_agent.py` `_CAPS` / `_escalate` — capital clamps. The veto is
  forced ON and `rebalance_allowed` forced OFF regardless of what the model says.
* `tests/test_skill_writer.py::test_execution_modules_never_import_the_writer` —
  fails the suite if `pov/`, `risk_gate`, `risk_engine`, `orchestrator`,
  `live_monitor`, `runner` or `ledger` ever imports the agent layer.

**"No cap" means no cap on judgement.** A skill may veto anything, blacklist
anything, or demand any evidence. It may not widen a magnitude that protects
capital — that clamp is the difference between an advisory layer and an
uncontrolled one, and it is what keeps the project's own protection order
(Capital → Data → Risk → Execution → Strategy → Optimization) true.

### 12.2 The data gate — "only data-satisfied logic moves on"

`core/stage_gate.py`. Deterministic Python, never an LLM, fail-closed: a
requirement that cannot be *evaluated* is a block, not a pass.

```json
"engines": { "stage_gate": { "enabled": false, "on_block": "halt_analysis" } }
```

| `on_block` | Behaviour |
|---|---|
| `halt_analysis` | Stages after the first starved station are not executed; `risk`, `infrastructure` and the POV block still run so the report is complete |
| `warn` | Every verdict is recorded, the chain runs unchanged |

Checks: `present`, `min_rows`, `min_count`, `range`, `one_of`, `enum_keys`, plus
optional freshness (`timestamp_path` + `max_age_seconds`). A naive or
future-dated timestamp is rejected.

### 12.3 The self-correction loop

`core/self_correction.py`. The model answers → a **deterministic judge** (schema
validator + data gate) decides → only a real failure produces a correction
prompt built from whitelisted fragments → a repeated identical failure stops the
loop.

```json
"engines": { "agent": { "self_correction": {
    "enabled": true, "max_attempts": 2, "tools": true } } }
```

* `max_attempts` is clamped 1..3 in code.
* Every attempt shares the ONE session call budget (`begin_session`), so a loop
  cannot drain the free tier.
* A **clamped** answer is a success and is not retried.
* `disabled` / `quota` / `llm_failed` are never retried.

### 12.4 The read-only toolbelt

`core/data_tools.py` supplies `list_sources`, `read_json`, `cycle_payload`,
`price_history`, `provider`, `data_catalog`. No write tool exists. `read_json`
is confined to `read_roots` (`data`, `output`), refuses traversal and anything
over 512 KB, and returns `{"error": ...}` instead of raising — the model chooses
the path, so a bad argument must be correctable, not fatal.

### 12.5 The shift ledger and the dashboard

Every run publishes `data/last_shift.json` (`dashboard/state.py`, atomic write)
and renders it at **Agent Line** (`/agent-line`). One row per station:
`worked` / `idle` / `blocked` / `failed` / `skipped`, plus the exact material the
next shift needs. Stations behind a stop are `skipped`, never blamed as
`blocked`; a station with no agent opinion is `idle` and the machine uses its
own defaults — the line only stops for missing **data**.

### 12.6 Operating checks

```powershell
cd C:\Users\Admin\CodeGPT\my-trading-system
# the whole agent layer
.\venv\Scripts\python.exe -m pytest Blue_engine\tests\test_stage_gate.py Blue_engine\tests\test_skill_writer.py Blue_engine\tests\test_stage_skills.py Blue_engine\tests\test_data_tools.py Blue_engine\tests\test_self_correction.py Blue_engine\tests\test_agent_team.py Blue_engine\tests\test_agent_line.py Blue_engine\tests\test_pipeline_stage_integration.py -q

# is the write sandbox still JSON-only? (should print a refusal)
.\venv\Scripts\python.exe -c "import sys;sys.path.insert(0,'.');from blue_engine.core.skill_writer import SkillWriter;print(SkillWriter().write_json('core/risk_engine.py',{'x':1})['reason'])"
```

**Current defaults:** `engines.agent.enabled = false`,
`engines.stage_gate.enabled = false`. Enabling either is an explicit operator
decision — paper-run first, because a gate halt changes which stages produce
signals.

### 12.7 Measured baseline — paper shift, 2026-10-26

`core/paper_shift.py` makes the measurement repeatable. It forces `dry_run=True`
(there is no flag to change that), defaults to offline mode (no LLM calls) and
redirects the infrastructure probes so a cycle costs ~6 s instead of ~25 s.

```powershell
.\venv\Scripts\python.exe -m blue_engine.core.paper_shift --cycles 3 --out Temp\paper_shift_baseline.json
```

First measured run (3 cycles, gate ON, tools ON, LLM chain OFFLINE):

| Metric | Value | Reading |
|---|---|---|
| `cycles_allowed` | **0 / 3** | The line never reached risk. |
| `gate_blocks` | `{economic: 3}` | First station starved in every cycle. |
| `advised_stage_runs` | 9 | 3 stages per cycle were advised. |
| `llm_unavailable_runs` | **9 / 9** | Every failure was a chain failure, **not** a bad answer. |
| `schema_rejected_runs` | 0 | No answer was ever rejected by the schema. |
| `mean_attempts` | 1.0 | Vacuous: no answer existed to correct. |
| `clamp_rate` | 0.0 | Vacuous, same reason. |
| `write_failures` | 0 | The JSON writer was never asked to refuse anything. |
| `mean_duration_seconds` | 6.1 | Down from 22 s before the probe redirect. |

**The honest conclusion: this run measured the GATE, not the self-correction
loop.** Attempts-per-stage and clamp rate remain unmeasured because no model
answered — they are `1.0` and `0.0` only in the vacuous sense.

**Finding 1 — the gate's default requirement is too strict for a cold start.**
`DEFAULT_REQUIREMENTS["economic"]` demands a `macro` key, but the pipeline is
designed to accept an `agent_inputs` *text* payload, not a macro snapshot. In
`halt_analysis` mode that blocks every analytic stage even though the agent
produced usable advice. Before enabling the gate in any window, either:

* supply a real `macro` block from the provider path, **or**
* override the requirement, e.g.
  `"stage_gate": {"stages": {"economic": [{"path": "agent_advice.economic",
  "check": "present"}]}}`.

**Finding 2 — offline mode cannot measure the correction loop.** To measure
`mean_attempts` and `clamp_rate`, run with `--live-llm` and a reachable Ollama
model. Offline is the right default for a fast, deterministic gate check, but it
is *not* a measure of agent quality — and the harness now says so in its notes.

**Finding 3 — a narrow telemetry bug, fixed.** The first version counted "no LLM
backend available" as a schema rejection, which would have hidden the real
blocker. `agent_telemetry[stage]["warning_kind"]` now separates
`llm_unavailable` / `quota` / `llm_failed` / `schema_rejected`.

**Next** (in priority order):
1. Re-run with `--live-llm` and record attempts/clamp rate — the only numbers
   that say whether the correction loop earns its call budget.
2. Decide the gate's cold-start requirement (Finding 1) against the real
   provider, then enable `stage_gate` in a paper window.
3. Only then consider `agent.enabled = true` in the live loop.

### 12.8 Agent memory — three memories, one writable place

`core/agent_memory.py`. Each station now remembers, and each memory has a
different lifetime and a different blast radius.

| Memory | Lifetime | Stores | Effect on the next cycle |
|---|---|---|---|
| **Active working** (`_memory/working.json`) | this shift + last 5 cycles | which station produced what, which evidence was fetched, what changed | "EVIDENCE ALREADY IN HAND — do not ask again"; "YOUR INPUT CHANGED" |
| **Historical calibration** (`_memory/calibration.json`) | rolling 50 obs/stage | proposed vs operative values, and whether the rails clamped | "this station normally produces …"; "your proposals were tightened in N% of cycles" |
| **Mistake** (`_memory/mistakes.json`) | capped 30 rows, ≤3 signatures/stage | the exact failure the deterministic judge produced + the fix that worked | "MISTAKES TO NOT REPEAT: [missing_keys x2] veto_reason absent" |

**Memory is context, never authority.** It enters the prompt and nothing else:
it cannot set an operative number, clear a veto, or bypass `_validate` /
`_escalate`. `test_memory_cannot_turn_a_bad_answer_into_a_good_one` and
`test_memory_never_overrides_the_safe_direction` pin that.

**What is never learned:** an infrastructure failure. `llm_unavailable`,
`quota` and `llm_failed` are not mistakes the model made, so they are not
taught (`test_an_infrastructure_outage_is_not_taught_as_a_mistake`).

**Storage rules.** All three files live under `<agent_io.sandbox>/_memory/` and
are written through `SkillWriter`, so the same refusals apply (`.json` only, no
traversal, no NaN). Every string passes `_clean`, which redacts
credential-shaped text. A corrupt file degrades to empty memory with a note; it
never raises.

```json
"engines": { "agent": { "memory": { "enabled": true } } }
```

Delete a file to forget that memory. There is no "reset" API on purpose: the
files are plain JSON so an operator can read and edit them.

### 12.9 Three defects found while building memory (all shipped broken before)

Memory was the first consumer that read the agent layer's *output shape*
closely, and that exposed three real bugs:

1. **The clamp was computed and thrown away.** `self_correction.diagnose()`
   detected that `_escalate` had tightened an answer, the loop reported
   `clamped: true`, and then returned the model's **raw** payload. An agent
   proposing a 0.04 slippage ceiling against a 0.01 baseline kept 0.04.
   Fixed: the judge now carries the clamped payload and the loop returns it.
   Regression: `test_the_loop_returns_the_clamped_payload_not_the_raw_proposal`.
2. **The judge contradicted the validator about `data_requests`.** The
   validator treats the optional block as allowed; the judge treated it as an
   *extra key* and failed every answer that legitimately asked for evidence —
   which would have made the self-correction loop retry valid answers forever.
   Fixed with `_META_KEYS`. Regression:
   `test_data_requests_do_not_count_as_an_extra_key`.
3. **`FrontlineAgent.advise()` never reported a clamp.** It compared the
   escalated dict against *itself* (after assignment), so the "clamped"
   warning could never fire. Fixed by comparing against the pre-clamp payload;
   `outcome["clamped"]` is now set. (The payload was still clamped — only the
   report was wrong.)

Also fixed: `MistakeMemory.record()` truncated with `str(d)[:120]`, bypassing
the redaction helper, so a credential-shaped string could be persisted
verbatim. It now goes through `_clean`.

**Lesson recorded:** the paper-shift baseline said "clamp rate 0.0" and that
was taken as "no clamping happens". It was actually "clamping is never
reported". A metric that cannot be non-zero is not a measurement.

---

## 13. Headless broker login — TOTP, scheduled (2026-10-26)

`Blue_engine/core/broker_auth.py` + `scripts/ops/scheduled_login.py`.

### 13.1 There are TWO logins, and they are not the same

Verified against the local OpenAlgo source, not assumed:

| leg | who | credential | endpoint |
|---|---|---|---|
| 1. OpenAlgo platform | the dashboard/API account | `OPENALGO_USERNAME` + `OPENALGO_PASSWORD` (+ `OPENALGO_TOTP_SECRET` **only if** that account has 2FA) | `POST /auth/login`, then `POST /auth/login/totp` |
| 2. Broker | the trading account | `BROKER_USERNAME` + `BROKER_PASSWORD` + `BROKER_TOTP_SECRET` | `POST /auth/broker/<BROKER_NAME>/callback` with `userid`, `pin`, `totp` |

Source of truth: `openalgo/blueprints/auth.py::login` / `::login_totp` /
`::broker_login`, and `openalgo/blueprints/brlogin.py::broker_callback`.
**`BROKER_TOTP_SECRET` is the broker's alphanumeric key** — not the OpenAlgo
account's. Mixing them up produces "Invalid TOTP" on the broker leg.

A successful login response is **not** proof of a usable session: the module
probes `/api/v1/ping` and reports `session_ready` separately.

### 13.2 The five `.env` values

```dotenv
BROKER_AUTOLOGIN=0          # opt-in. 0 = scheduled tasks refuse to log in
OPENALGO_URL=http://127.0.0.1:5000
OPENALGO_USERNAME=
OPENALGO_PASSWORD=
OPENALGO_TOTP_SECRET=       # optional; only if the OpenAlgo ACCOUNT has 2FA
BROKER_NAME=                # broker slug used in the callback URL
BROKER_USERNAME=
BROKER_PASSWORD=
BROKER_TOTP_SECRET=         # the alphanumeric Base32 seed
```

Resolution order is the project's existing one: process env → `_restored/.env`
→ `.env`. Only the **names** are ever printed; `redact()` shows `***<tail>` and
the length at most. Codes are computed in memory, used, and dropped — they are
never written to a result, log or run record.

### 13.3 Running it

```powershell
python scripts\ops\scheduled_login.py --status   # creds present? never logs in
python scripts\ops\scheduled_login.py --check    # preflight only
python scripts\ops\scheduled_login.py            # the real thing (needs opt-in)
python scripts\ops\scheduled_login.py --enable    # one-off opt-in
```

| exit | meaning |
|---|---|
| 0 | session ready (both legs + verify) |
| 2 | not opted in (`BROKER_AUTOLOGIN` != 1) |
| 3 | config problem (missing credential, unusable TOTP seed) |
| 4 | OpenAlgo not reachable |
| 5 | platform login failed |
| 6 | broker login failed |
| 7 | login answered, session probe failed |

Writes `Temp/run/last_login.json` and appends `Temp/run/login_history.jsonl`.

### 13.4 Scheduling it

**Windows Task Scheduler** — import the shipped XML after replacing
`__REPO_ROOT__` with the absolute path:

```powershell
schtasks /Create /TN "BlueEngine\BrokerLogin" /XML scripts\ops\broker_login_task.xml /F
```

The XML ships: Mon–Fri **08:50**, `StartWhenAvailable`, `RunOnlyIfNetworkAvailable`,
3 × 2-min restart-on-failure, 5-minute execution limit, `LeastPrivilege`.

**cron (Linux/WSL):**

```cron
50 8 * * 1-5  cd /path/my-trading-system && .venv/bin/python scripts/ops/scheduled_login.py >> Temp/run/login_cron.log 2>&1
```

`SCHEDULED_LOGIN.cmd` exists because Task Scheduler runs with **no working
directory and no console**: it derives the repo root from its own location,
calls the venv interpreter by absolute path, and logs to
`Temp/run/login_task.log`.

**Why a clock trigger and not a boot trigger.** A login at boot can land hours
before the open with a token that expires before it matters; a login before
OpenAlgo is up just fails leg 1. 08:50 is 25 minutes before the 09:15 open and
after the launcher has had time to start the stack.

### 13.5 Safety rules encoded in code

1. **Opt-in.** No `BROKER_AUTOLOGIN=1` → exit 2, nothing attempted. A task must
   not start logging in because a file appeared.
2. **Fail closed, name the leg.** Each leg reports itself; a failure stops the
   flow. Nothing retries in a loop — retry policy is the scheduler's job.
3. **Leg isolation.** A failed platform login never reaches the broker
   callback, so a wrong password cannot burn broker login attempts and lock the
   account (`test_a_failed_platform_login_never_reaches_the_broker`).
4. **Fresh code.** The TOTP is computed immediately before the POST, and the
   flow waits for the next window if fewer than 5 s remain — a code sent with
   1 s left fails and still counts as a login attempt.
5. **Never trades, never starts OpenAlgo, never edits `openalgo/`.**
6. **A dead session is not a success.** A 200 from login is verified by probe.

### 13.6 Defect found while building this

`verify_session()` reported a failed probe against the *previous* leg
(`result.leg = result.leg or "verify"` — and `leg` was always already set), so a
probe failure was blamed on the broker TOTP. Pointed at the wrong fix. The
probe is now its own leg. Regression:
`test_a_login_200_with_a_dead_session_is_not_trusted`.

---
*This SOP documents verified behaviour only. Items marked OPEN in the audit are
known gaps, not guarantees.*

---

## 14. Ubuntu VPS: one-click launch, desktop, remote access (2026-10-27)

Three scripts. Only the first is new behaviour; the other two automate what
this SOP already documents by hand.

| Script | Role |
|---|---|
| `Servers/launch_ubuntu_server.sh` | **one click.** Stages: preflight → `install_ubuntu.sh` → `provision_desktop_remote.sh` → 08:45 pre-open timer → 16:00 backup cron → verify |
| `Servers/provision_desktop_remote.sh` | XFCE, xrdp, Google Chrome, Node LTS + PM2, UFW, boot stack (openalgo / openalgo-ws / pov-dash) |
| `scripts/ops/preopen_launch.sh` | gated pre-open start: clock → lock → platform → login → smoke → engine |

```bash
sudo bash Servers/launch_ubuntu_server.sh --rdp-allow 203.0.113.7/32
```

### 14.1 Rules this implements (do not relax them)

1. **systemd is the supervisor of record.** PM2 is installed (requested) and its
   boot hook is set, but no trading process is registered with it. Two
   supervisors over one port is how a duplicate order is placed.
2. **`pov-runner` never auto-starts at boot.** `--start-engine` and the 08:45
   timer start it behind a gate; a reboot does not. AGENTS.md §Automatic
   Recovery.
3. **UFW fails safe.** 22/tcp is allowed *before* `ufw enable`; 3389 is opened
   only for the CIDR given with `--rdp-allow`; 80/443 stay closed until TLS.
4. **The pre-open chain is fail-closed.** Exit 10/11/12/13/14/15 all mean the
   engine did not start. `log/preopen/last_preopen.json` records which gate.
5. **No secrets added anywhere.** Both new scripts read `Servers/deploy.env`
   with the same non-`source` parser `install_ubuntu.sh` uses. No credential is
   written to any file they create.

### 14.2 Facts that are easy to get wrong

| Claim | Reality |
|---|---|
| "Install WinSCP on the Ubuntu server" | WinSCP is Windows-only. Use WinSCP **on the laptop** with an SFTP session, or `apt-get install filezilla` on the server. |
| "XFCE + xrdp just works" | No. `/etc/xrdp/startwm.sh` must `exec startxfce4` or every session is black. `xorgxrdp` must be present. The colord polkit prompt blocks the first login. All three are handled in `provision_desktop_remote.sh`. |
| "cron is enough for the 08:45 start" | The default is a systemd timer: `Persistent=true`, journald logging, `RandomizedDelaySec`. The cron line is written but commented. Use one, never both. |
| "pm2 saves the day" | `pm2 startup` gives a user-scoped daemon. It does not know about `_restored/`, systemd dependencies, or the smoke gate. Use `systemctl`. |
| "RDP is safe on 3389 worldwide" | RDP has a long history of pre-auth RCE. Either restrict to your IP or tunnel over SSH (`ssh -L 3389:127.0.0.1:3389`). |

Verified at authoring time: `bash -n` clean on all three scripts. **Not run**
against a live Ubuntu VPS from this host - the package/unit names follow the
Ubuntu 22.04/24.04 archive, but treat the first real run as the acceptance
test and read every `[WARN]`/`[FAIL]` line.

---

## 15. Dependencies: which file owns which environment (2026-10-27)

`requirements.txt` in the repo root now exists. Before this it did not, yet
`start_all.py --update`, `upgrade_openalgo.ps1` and `START_ALL.cmd` all
named it, so every dependency refresh failed silently into a warning.

| Environment | File | Installs |
|---|---|---|
| Windows / dev host, root `.venv` | `requirements.txt` (this repo) | openalgo SDK, Dash 4 + DMC, pandas/numpy/pyarrow, sklearn/statsmodels/scipy, optuna, screener stack, pyotp, yfinance, pytest, **eventlet** |
| Ubuntu VPS | `openalgo/requirements.txt` (upstream, pinned) + `blue_engine[dashboard]` | the platform's own stack; the engine only as an editable package |

**Rules**
1. Every pin in `requirements.txt` was read from installed distribution
   metadata on this host (2026-10-27), not recalled from memory.
2. `eventlet` is pinned deliberately: a stock venv gets no async SocketIO
   server, which is why `start_openalgo.py` has to pass
   `allow_unsafe_werkzeug=True`. `gunicorn` is *not* installed on Windows
   (it needs `fcntl`).
3. `pyotp` is required, not optional. `core/broker_auth.py` imports it
   lazily, so its absence does not stop the engine - it makes every 08:45 login
   fail. Same class of bug as a missing `_restored/.env`.
4. `pydantic` is bounded (`>=2.6,<3`) rather than patch-pinned: the exact
   build resolved here has no cached wheel on an offline resolver.
5. matplotlib, kaleido, TA-Lib and pandas_ta are deliberately absent - the code
   implements indicators by hand and renders every figure in the browser.
6. Do not add engine deps here *and* in `pyproject.toml`. `requirements.txt`
   mirrors the extras; `pyproject.toml` stays the packaging truth.

Verified: `pip install --dry-run --no-index -r requirements.txt` resolves
clean against the current `.venv` (python 3.13.15).

## 16. Token optimization — dev-side and runtime (2026-10-29)

Two separate budgets. Fixing one does not fix the other.

### 16a. Dev-side (IDE / CodeGPT / OpenRouter billing)

Rules live in `rules/token-optimization.md` and are enforced by convention:
no whole-file reprints, line-range reads, surgical edits, ≤5-word comments,
max 3 mock rows, no filler prose.

Editor scoping in `.vscode/settings.json`: `search.exclude`,
`files.watcherExclude`, `python.analysis.exclude` now skip `.venv`, `data/`,
`db/`, `log/`, `logs/`, `_restored/`, `patches/`, `Temp/`, node_modules,
build outputs. Fewer indexed bytes per request = smaller retrieved context.

### 16b. Runtime (frontline agent → Ollama / OpenRouter)

New module: `Blue_engine/core/llm/tokens.py` (`TokenGovernor`, `PromptCache`,
`TokenMeter`, `cap_messages`, `compact_text`). Wired into
`LLMClient.from_config`; every `complete()` goes through it.

| Control | Default | Effect |
|---|---|---|
| Prompt cap | 3072 tokens (600 output) | system messages never dropped; user text cut with a visible `[... elided N chars ...]` marker |
| Response cache | 900 s TTL, 256 LRU entries | identical prompt+model+chain+session → same stored answer, zero new call |
| Input ceiling | `_MAX_INPUT_CHARS` 6000 → 3000 | each engine call cost less; up to 8 calls per run |
| Metering | always on | `client.status()["tokens"]` → calls, hits, tokens saved, compactions, per-backend split |

Config block (`engines.llm.tokens`) — all keys optional, governance is ON by
default and disabled only explicitly:

```json
"tokens": {"enabled": true, "max_prompt_tokens": 3072,
           "max_tokens_out": 600, "cache_ttl_seconds": 900,
           "cache_entries": 256}
```

### Verify

```powershell
cd C:\Users\Admin\CodeGPT\my-trading-system
.\.venv\Scripts\python.exe -m pytest Blue_engine/tests/test_llm_token_governor.py -q
.\.venv\Scripts\python.exe -c "from blue_engine.core.frontline_agent import FrontlineAgent;print(FrontlineAgent({'enabled':True}).client.status()['tokens'])"
```

### Rules that must not change

1. Governance may only **shrink** prompts and **skip duplicate** calls. It may
   never drop a system message, widen a schema, or cache a failed call.
2. Token estimates are for budgeting only — never a trading input.
3. Frontline only. `risk`, `execution`, `slice`, `pov` must not import
   `core/llm/*`.
4. Cache correctness depends on `temperature=0`. Raising temperature for the
   agent invalidates the cache reasoning; set `tokens.cache_ttl_seconds: 0` if
   you do.

## 17. Anthropic-first agent chain + LLM cost panel (2026-10-29)

### 17a. Backend order (first agent)

    anthropic -> ollama -> openrouter -> disabled

1. **Anthropic is tried first** when `ANTHROPIC_API_KEY` (or `llm.anthropic.api_key`)
   is present. Default model `claude-3-5-haiku-latest`; override with
   `llm.anthropic.model` or `ANTHROPIC_MODEL`.
2. **Automatic Ollama fallback.** No key, no subscription, no credit, a revoked
   key, a 429, or an unreachable API → the router skips Anthropic and the next
   backend answers *on the same call*. No retry loop, no raised exception.
3. **Retirement rules.** 401 / 402 / 403 / 429, and a 400 whose body mentions
   credit / billing / quota / subscription, retire Anthropic for the rest of the
   UTC day. A network failure does **not** retire it.
4. **No cross-provider key leaks.** The secret lookup is name-scoped; an
   OpenRouter key is never passed to Anthropic.

Wire format differs from OpenAI and is handled in `AnthropicBackend`:
`system` is a top-level string, `max_tokens` is mandatory, auth is `x-api-key`
plus `anthropic-version`, and the text is joined from `content[].text`.
The Messages API has **no** `response_format`; structured output therefore
relies on the system instruction plus the caller's schema validator, exactly
as with Ollama.

Verification:

```powershell
.\.venv\Scripts\python.exe -c "from blue_engine.core.frontline_agent import FrontlineAgent;print(FrontlineAgent({'enabled':True}).client.status()['chain'])"
```

Expect `['anthropic','ollama']` with a key, `['ollama']` without.

### 17b. LLM cost / cache panel

Page **Agent Line** (`/agent-line`) now carries a read-only *LLM cost / cache*
panel: calls, cache hits with hit-rate, prompt tokens saved, tokens spent, plus
one row per backend (position in chain, model, `ready` / `no credential` /
`retired today`).

Data path: `TokenGovernor.status()` → `pipeline` publishes
`dashboard.state.publish_token_usage` → `data/last_token_usage.json` →
`read_token_usage` → panel. Same atomic write and same honesty rule as the
shift ledger: **no run ⇒ "not measured"**, never a fabricated zero.

### Rules that must not change

1. The panel and the handoff are read-only. Neither can place, size or cancel
   an order, and neither touches the broker.
2. A failed token publish adds a `report["warnings"]` entry; it never fails a run.
3. Anthropic answers are cached only while the whole chain is intact — a
   fallback Ollama answer is never stored under the full-chain cache key.

## 18. LLM transport migration — complete (2026-10-29)

Every LLM caller in the engine now shares one transport. No module owns HTTP
any more.

| Site | Before | Now |
|---|---|---|
| `core/frontline_agent` | chain + governor | unchanged (reference implementation) |
| `sectors/sector_engine` (`SectorAdvisor`) | OpenRouter + native Ollama `/api/chat` | `LLMClient` over two members |
| `economics/nlp/llm_scorer` | OpenAI + Ollama `/v1/chat/completions`, preferred Ollama | `LLMClient` (Ollama → OpenRouter) |
| `portfolio/views/llm_note` | `openai` SDK + native `/api/generate` | `LLMClient` (Ollama → OpenRouter) |

**What is deliberately unchanged** — do not "clean these up":
1. The on-disk content-hash caches (`data/llm_cache`, `data/portfolio_llm`).
2. Every prompt string and every JSON schema validator.
3. The `method` vocabulary in each return value (`llm`/`cached`/`disabled`/
   `invalid_response`/`error`/`template`/`safety_fallback`).
4. `SectorAdvisor`'s 4-second deadline, 32 KB body cap, credential gate and
   cloud-first/local-second order.
5. The `http_post` probe injection in `LLMScorer` (routes through a probe
   backend, same request/response shape).

`LLMClient.for_named_site(...)` is the constructor these sites use: it gives
them the failover chain with the **in-memory governor cache off** (they already
have a disk cache; two caches would only add ambiguity). Opt in with
`{"tokens": {"enabled": true}}` in their config.

### Two latent bugs fixed on the way

1. **`llm_scorer` could not be imported.** It used `...providers.secrets`
   (`blue_engine.providers`, which does not exist). The economic engine imports
   it lazily, so the failure appeared only on first use. Now `..providers.secrets`.
2. **`llm_scorer.score()` always raised KeyError.** `_PROMPT_TEMPLATE` held
   literal JSON braces and was passed through `str.format`, so
   `KeyError('"hawkish"')` fired before any request was sent — the scorer had
   silently never run. Braces are now doubled.

### Behaviour changes to know about

- `llm_scorer` no longer *prefers* Ollama when `OLLAMA_HOST` is set; it follows
  the chain (Ollama first, only because it has no quota to spend).
- Ollama is reached through its OpenAI-compatible `/v1/chat/completions`
  instead of the native `/api/chat` / `/api/generate`. Same model, same JSON;
  `test_sector_engine.py` asserts the new URL.

### Verify

```powershell
.\.venv\Scripts\python.exe -m pytest Blue_engine/tests/test_llm_site_migration.py -q
```

## 19. Repo layout pass (2026-11-05)

Plain-English summary: folders now follow the tree in `AGENTS.md`. No trading logic changed.
Full plan and open decisions: `docs/REPO_LAYOUT_PLAN.md`.

**What moved (operators: update any bookmark or script that uses the old path):**

| Old path | New path |
|---|---|
| `dashboard/algo_pro/index.html` | `dashboard/index.html` |
| `dashboard/algo_pro/flow_svg.js` | `dashboard/assets/flow_svg.js` |
| `dashboard/algo_pro/make_flow_svg.py` | `scripts/ops/make_flow_svg.py` (same output, verified byte-identical) |
| `src/quant/models/events.py` | `src/quant/models/events/scoring.py` (`quant.models.events` import still works) |
| `docs/PHASE0_REUSE_AUDIT.md`, `docs/Economic_to_POV_Pipeline_Audit_2026-10-22.md`, `docs/ML_SKILL_DIAGNOSIS.md` | `docs/audits/` |
| `Blue_engine/skills/` | `docs/skills/` (documentation only; code defines its skills in Python) |
| `strategies/.gitkeep` (root) | removed; real strategy configs stay in `config/strategies/` |

**What was added:** `pyproject.toml` (metadata and pytest test paths), `src/quant/strategies/` (scaffold).

**Not moved (waiting for owner decision, see `docs/REPO_LAYOUT_PLAN.md` section 5):**
`Servers/` (live VPS keeps `deploy.env` there), `quant_system_flow_v5.svg` location,
this SOP's location, `scripts/diagnostics/` and `scripts/verify/` (depth-based paths).

**Pre-session checks reality (sandbox, 2026-11-05):**
- `pytest tests/unit` → 239 passed, 70 failed, 34 skipped, 1 xfailed. Same 70 failures before and after the pass; all are `No module named 'openalgo'`.
- `pytest Blue_engine/tests --collect-only` → 176 collected.
- `tests/unit/test_layout.py` → 4 passed.
- The OpenAlgo package cannot be installed in the cloud sandbox (package installs are off). Full regression must run on the owner's machine.

## 20. FastAPI dashboard (2026-11-05)

Plain-English summary: a web page served by FastAPI. Reads are open; writes go only through token-protected POST routes (rule revised 2026-11-10, see below). It shows mode, kill switch,
risk limits, positions, realized and unrealized P&L, win rate, profit factor, risk use,
safety events and rule-based "AI insight" lines. It never sends orders.

**How to run (owner machine, after `pip install -r requirements.txt` with FastAPI and uvicorn):**
```
python dashboard/app.py
```
Open `http://127.0.0.1:8050`. Data is read from `data/paper/` (ledger, audit log, kill switch) and `config/risk_limits.yaml`.
Missing files show an empty, safe state.

**Rules built in:** P&L is shown by default (`config/settings.yaml`, key `dashboard.pnl_visible_default`).
EV, MAE/MFE and rule compliance are not in the payload (AGENTS rule 8). POST routes exist only behind the token (`DASHBOARD_API_TOKEN`). Revised rule (2026-11-10, owner): the dashboard may change the pre-open schedule switch and manage named dashboard accounts. It never places, sizes or cancels orders and never starts the engine directly - the engine starts only through the schedule gates. Feed status says "not connected" (no live feed exists).

**Not verified in the cloud sandbox:** FastAPI routes and the page in a browser (FastAPI cannot be installed there).
Profit factor with closed trades needs `openalgo` (Blue_engine). The test for it skips until `openalgo` is installed.

## 21. Blue terminal console removed (2026-11-05)

Plain-English summary: the `rich` terminal console (`Blue_engine/dashboard/`, command
`python -m blue_engine.dashboard.cli`) is removed from the repo. Copies are in `Temp/archive/2026-11-05/`
(git-ignored) and in git history. Plan: `docs/DASHBOARD_REMOVAL_PLAN.md`.

**What operators must know:**
- VPS: the `pov-dash` service now runs the FastAPI dashboard (`uvicorn ... dashboard/app.py`) on the same port. The nginx route `/pov-dash/` is unchanged. Run `install_ubuntu.sh` again (or `sudo systemctl daemon-reload && sudo systemctl restart pov-dash`) to apply the new unit.
- Installers: `Blue_engine[dashboard]` is now `Blue_engine[charts]` (plotly, pandas, numpy). `rich` is no longer installed. `fastapi` and `uvicorn` are installed.
- Run data files `data/last_shift.json` and `data/last_token_usage.json` are no longer written. Token usage is still in the run report as `llm_tokens`.
- `Blue_engine/Dockerfile` (image for the terminal console) is removed. If a compose file outside this repo still builds it, update that file.
- Preset watchlists: `resolve_watchlist` moved to `Blue_engine/screener/watchlist.py` unchanged. Known gap: `blue_engine/screener/presets.py` is not in the repo, so a preset name returns no rows (this was already true before).

**Not run here:** the Blue_engine test suite (needs `openalgo`), the VPS installer, the Windows installers, and Docker.

## 22. One script per OS: install, update, start (2026-11-07)

- **Windows:** `install.bat` (double-click). Updates the repo, installs what is
  missing, runs the safe OpenAlgo upgrade when OpenAlgo exists, then starts the stack.
- **Ubuntu:** `Servers/launch_ubuntu_server.sh`. Clones the repo if missing,
  installs the platform, or does a gated update when it exists (refuses in NSE
  hours or while `pov-runner` runs). Restarts `openalgo`, `openalgo-ws`, `pov-dash`
  only.
- Retired (moved to `Temp/archive/2026-11-07/installers/`): `install.sh`,
  `scripts/ops/START_ALL.cmd`, `scripts/ops/START_OPENALGO.bat`.
- Kept as shims for scheduled tasks: `scripts/start_openalgo.py`, `scripts/upgrade_openalgo.ps1`.
- Not run here: the Windows `.bat` and a real Ubuntu install. Owner test pending.

## 23. Scheduled starts (2026-11-07)

- Windows: `scripts/ops/register_start_jobs.ps1` creates two Task Scheduler jobs for the
  current user. "MTS - start at logon" and "MTS - pre-open start" (Mon-Fri 08:40, wake to run).
  Both run `install.bat --scheduled`: start only, no code update, no engine.
- Ubuntu: `mts-boot-start.service` runs `launch_ubuntu_server.sh --start-only` at every boot.
  The existing `pov-preopen.timer` (08:45) remains the only scheduled engine start.
- Scheduled runs never update code. Updates stay manual.
- Not run here: the PowerShell script (no `pwsh` in the sandbox) and the boot unit (no systemd). Owner test pending.

## 24. Capital-protection gates, live data source, NSE inputs (2026-11-11)

### 24.1 Live data source (approved)
- `config/data_sources.yaml` now sets `sources.live_feed.live_allowed: true`, approved by the
  owner on 2026-11-11. The source is the **broker's own market-data feed through OpenAlgo**,
  the same licensed connection that carries orders. No third-party scraper prices an order.
- `sources.nse_public` (nsepython) is `live_allowed: false`: delayed/end-of-day NSE public data,
  used for calendar, deals, events, FII/DII, index and beta inputs only.
- The bot still refuses to run a cycle unless `live_allowed` is true, and still refuses live
  mode in code (`live_enabled` must stay false; a human `LiveApproval` is required).

### 24.2 OpenAlgo bridge and broker connection
- `config/data_sources.yaml` → `openalgo:` block: `base_url`, `api_key_env`, `mode`,
  `broker_login` (autologin, broker name, TOTP env) and `compliance` (algo ID, SEBI
  registration, audit dir, max clock skew).
- Secrets stay in `.env` (`OPENALGO_API_KEY`, `BROKER_*`, `SEBI_ALGO_ID`, `SEBI_REGISTRATION`).
  Never commit them.
- Two login legs must both pass before an order can be placed: OpenAlgo platform login and
  broker TOTP (`Blue_engine/core/broker_auth.py`).

### 24.3 Pre-gate safety (before the risk gate)
`src/quant/risk/pre_gate.py`. Runs on every **entry**; exits bypass it. It can only refuse.
| Check | Rule | Fail-closed behaviour |
|---|---|---|
| Trading calendar | NSE equity holiday CSV (`config/calendar/nse_holidays_2026.csv`) + weekend | unreadable calendar refuses the day |
| Event blackout | results / RBI / Budget / Fed dates | blocked date refuses the entry |
| Overnight gap | entry ref price vs previous close, default 10% | gap beyond limit refuses the entry |
| Trade-intent handoff | ids, symbol, qty, price, side, kind, lot size, long-only | any bad field refuses |
| Stress / VaR | whole book shocked by `shock` (default -10%), loss vs `var_limit` (default 5% of capital) | no capital known refuses |

Wiring: `StrategyBot._scan` runs calendar + handoff before sizing; `gate_signal` runs the full
check on the real intent (correct ref price and size) before `check_intent`.

### 24.4 Compliance gate (live only)
`src/quant/risk/compliance.py`, called from `OpenAlgoClient._check_live_session`.
| Check | Requirement |
|---|---|
| SEBI algo tagging | exchange-approved algo ID + SEBI registration number on file |
| Audit-log storage | audit dir exists, is writable, has free space |
| NTP clock sync | skew within `max_clock_skew_seconds` (default 1.0 s) |

Any failure raises `PermissionError` and the live order is not sent. Paper and analyze are not
gated (no real order leaves the machine).

### 24.5 Daily loss counters (restart-safe, mark-to-market)
- `src/quant/execution/pnl_store.py` persists `daily_pnl` and per-strategy P&L per day.
  A restart reloads today's counters and re-trips the kill switch if the day already breached.
- `StrategyBot._mark_positions_to_market` adds the unrealized P&L of open delivery positions
  to the counters every cycle, so a falling book trips the daily limit before the stop realizes.

### 24.6 Screener output file
- `quant.data.screener_engine.write_screen_file(payload, "data/screened/latest.json")` writes
  the bot's screen file atomically. Rows without a usable circuit limit, or with an ASM/GSM
  flag, are dropped. `select_universe` (Blue wrap) supplies the ordering.

### 24.7 OCL strategies in the bot
- `quant.strategies.ocl.adapter.OclStrategyAdapter` serves the bot's closed bars to the OCL
  classes through a local `BarsHistoryClient` (no network). Selectable in `config/bot.yaml`:
  `ocl`, `ocl_sell_fail_top`, `ocl_sell_fail_bottom`.

### 24.8 NSE public data (nsepython)
- `quant.data.nse_api` wraps nsepython 2.101: `nse_holidays`, `nse_blockdeal`,
  `get_bulkdeals`, `nse_annual_reports`, `nse_events`, `nse_fiidii`, `nse_get_index_quote`,
  `index_history`, `nse_circular`, `get_beta`. Function names were read from the installed
  package, not guessed. Every call raises `NseUnavailable` on failure.
- Install: `py -m pip install nsepython` (already in the host environment).

### 24.9 Pre-session checks added
| # | Check | Pass looks like |
|---|---|---|
| 7 | Live data source approved | `live_feed_approved("config/data_sources.yaml")` is `True` |
| 8 | Trading calendar readable | `PreGate().calendar_ok(today).ok` is `True` |
| 9 | Compliance (live only) | `ComplianceGate(...).check().ok` is `True` |
| 10 | Screen file fresh | `data/screened/latest.json` `as_of` within `max_screen_age_minutes` |
