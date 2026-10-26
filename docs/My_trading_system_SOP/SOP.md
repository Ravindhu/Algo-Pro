# My Trading System — Standard Operating Procedure

**Last updated:** 2026-10-26
**Applies to:** `C:\Users\Admin\CodeGPT\my-trading-system\Blue_engine`
**Companion document:** `docs/Economic_to_POV_Pipeline_Audit_2026-10-22.md`

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

# ...or double-click:
scripts\ops\START_ALL.cmd
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

---
*This SOP documents verified behaviour only. Items marked OPEN in the audit are
known gaps, not guarantees.*
