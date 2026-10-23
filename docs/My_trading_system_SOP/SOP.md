# My Trading System — Standard Operating Procedure

**Last updated:** 2026-10-23
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
| India VIX | **vendor parquet** | **check `stale`** — lagging since 2026-10-12 |
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

> **Current state: the vendor pin is behind.** Last FII/DII row **2026-10-13**,
> last VIX **2026-10-12**. That is ~7 trading days. Expect
> `vendor flows STALE` in the warnings until the pin is updated.

To update the data, refresh the vendor checkout:

```powershell
cd Blue_engine\database\vendor\indian-market-historical-data
& 'C:\Program Files\Git\cmd\git.exe' sparse-checkout set scripts data/FLOWS data/MACRO data/INDICES
& 'C:\Program Files\Git\cmd\git.exe' pull --ff-only
```

> The clone is **sparse** (scripts only by default). `data/` is intentionally not
> checked out — that is why the data appeared to be missing. Again: the files
> exist in the repo; they are simply excluded from the working tree.

**Known data caveats in this checkout:**

| File | Caveat |
|---|---|
| `MACRO/cpi_inflation.parquet` | last print **2025-03** — use the live MoSPI provider, not this file |
| `INDICES/*.parquet` | newest bar is often **partial** (Close = NaN, Volume = 0); drop NaN closes |
| `FLOWS/*.parquet` | lagging; always read `meta.stale` |

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

*This SOP documents verified behaviour only. Items marked OPEN in the audit are
known gaps, not guarantees.*
