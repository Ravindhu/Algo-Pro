# Scripts

Operational tooling for the trading system. Everything here is **read-only with
respect to your money** unless explicitly stated — no script in this folder
places, modifies or cancels an order.

Run all Python scripts with the repository venv, from the `my-trading-system/`
folder:

```powershell
.\\.venv\\Scripts\\python.exe .\\scripts\\<folder>\\<script>.py
```

## Layout (grouped 2026-10-21)

| Folder | What lives there |
|---|---|
| `run/` | Things that **start** something: `run_pov`, `run_screener_bridge`, `run_economic_engine`, `run_real` |
| `ops/` | Platform lifecycle + schedules + QA: `start_openalgo`, `upgrade_openalgo.ps1`, the two `register_*.ps1` scheduled-task scripts, `qa_*.mjs` |
| `verify/` | Health and contract checks: `smoke_test`, `probe_*`, `verify_*`, `check_*`, `test_*` |
| `train/` | Offline model/strategy work: `ml_train`, `optimize_strategy`, `backtest_ocl_confirm` |
| `research/` | Offline analysis: `e2e_replay`, `make_real_levels` |
| `data/` | Fetches/audits reference data: `fetch_infy*`, `audit_macro_values` |
| `diagnostics/` | Source-discovery probes (see below) |

**Compatibility shims.** Documented entry points (`run_pov.py`,
`run_screener_bridge.py`, `smoke_test.py`, `probe_sdk.py`, `start_openalgo.py`,
`optimize_strategy.py`, `run_economic_engine.py`, `e2e_replay.py`) still exist at
their old `scripts/` path as thin shims. They `runpy` the real module with
`sys.argv` untouched, so every command in the SOP, the Ubuntu deploy docs, the
systemd units and the scheduled tasks keeps working byte-for-byte. New code
should import/reference the folder path directly.

---

## Start / operate

| Script | Purpose |
|---|---|
| `ops/start_openalgo.py` | **Main launcher.** Starts the WS proxy then the web app. Restores `_restored/` state into the pristine `openalgo/` clone first. |
| `run/run_economic_engine.py` | Runs one economic pass and appends it to the run history. **Writes** to `data/economic_runs/`. |
| `run/run_pov.py` | Runs the POV engine (read-only analysis). |
| `run/run_screener_bridge.py` | Screener list -> OCL signal per symbol -> RiskGate -> POV. **Places orders** unless OpenAlgo is in analyzer mode. |

## Verify — run these to confirm the system is healthy

| Script | Purpose |
|---|---|
| `verify/verify_openalgo.py` | Probes 20 OpenAlgo REST endpoints. Documents the real auth contract: `apikey` goes in the **JSON body** for POST, `?apikey=` for GET. A Bearer header is silently rejected. |
| `verify/verify_strategy_api.py` | Strategy / portfolio subsystem endpoints. |
| `verify/verify_live_stack.py` | **The one that matters.** End-to-end check that the live macro providers actually resolve and the values are plausible. Supersedes the older per-provider verifiers. |
| `verify/check_dbs.py` | `PRAGMA integrity_check` across all five SQLite databases. |
| `verify/smoke_test.py` | Broad smoke test of the engine package. |

## Test — offline, no network
| Script | Purpose |
|---|---|
| `verify/test_mospi_provider.py` | 31 assertions. Pins the two `rows[0]` traps: CPI returns Rural first (the headline is Combined), and PLFS returns rural-male-2022 first. |
| `verify/test_rbi_provider.py` | 7 assertions on the FBIL parser contract. |
| `verify/probe_rbi_reference_rate.py` | Confirms the rbi.org.in homepage still parses for the reference rate. |
| `verify/probe_sdk.py` | Inspects the installed OpenAlgo SDK surface. Resolves the API key from the repo `.env` (falls back to `_restored/.env`). |

## Data
| Script | Purpose |
|---|---|
| `data/audit_macro_values.py` | Every macro value against verified ground truth, plus a record of which sources were **ruled out and why**. Run this before trusting any macro number. |
| `data/fetch_infy.py` / `data/fetch_infy_daily.py` | Writes `data/infy_1min.csv` / `data/infy_daily.csv` (research inputs for `research/` and `diagnostics/`). |

---

## `diagnostics/`

Discovery probes used while reverse-engineering data sources. Kept because each
one records *why* a source was rejected — re-deriving that costs hours.

`diagnostics/superseded/` holds verifiers made redundant by
`verify_live_stack.py`. They still run; they are simply narrower. Kept for
reference rather than deleted, since each one contains the working endpoint
shape for a source we may revisit.

> **Archived 2026-10-21:** ~240 loose files that had accumulated in `Temp/`
> (one-off scripts, scrape dumps, run logs, PDFs) were moved to
> `Temp/archive/2026-10-21/<category>/` with an `index.txt` recording every
> former path. Restore anything from that index; `Temp/` is now staging-only.

### Sources investigated, and the verdict on each

| Source | Verdict |
|---|---|
| **MoSPI eSankhyiki** | ✅ **IN USE.** Official, documented, no API key. Publishes `llms.txt` + `ai-catalog.json` and runs a public MCP server. Supplies inflation, GDP, employment. |
| **rbi.org.in homepage** | ✅ **IN USE.** Plain HTML, parses for the FBIL USD/INR reference rate. |
| data.rbi.org.in (DBIE) | ❌ Undocumented SPA. The SAP token will not issue to a script (needs a browser fingerprint); data calls return `errorCode 4302`. |
| RBI WSS pages | ❌ HTTP 200 but only a 3.4 KB JS template — no number in the HTML. |
| FBIL / CCIL | ❌ Render via JavaScript; no data endpoint. |
| Statistics of the World | ❌ **Dangerous.** `?country=IND` is *silently ignored* — it returns world-rank #1, so `data[0]` was Venezuela (387% inflation), not India. |
| NSE `allIndices` | ⚠️ Works, but exposes only price **index levels**. `NIFTY GS 10YR` is 2644, not a ~6.68% yield. |
| FRED | ❌ Unreachable from this host. |
| OpenAlgo SDK | ⚠️ Works for commodities/indices. The `INDIAVIX` quote is internally inconsistent (`ltp` 38.88 vs `prev_close` 12.16), so it is not used. |

### Why this matters

Every rejection above was a source that *looked* like it worked — HTTP 200,
valid JSON, plausible shape — and returned wrong data. The two worst cases:

- **SOTW** would have fed Venezuela's hyperinflation into an India regime score.
- **PLFS `rows[0]`** reported 1.9% unemployment — a real figure for rural males
  in 2022, presented as the national rate.

Both are documented in the code that now guards against them.