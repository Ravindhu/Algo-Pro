# Scripts

Operational tooling for the trading system. Everything here is **read-only with
respect to your money** unless explicitly stated — no script in this folder
places, modifies or cancels an order.

Run all Python scripts with the OpenAlgo venv, from the `my-trading-system/`
folder:

```powershell
.\openalgo\.venv\Scripts\python.exe .\Scripts\<script>.py
```

---

## Start / operate

| Script | Purpose |
|---|---|
| `start_openalgo.py` | **Main launcher.** Starts the WS proxy then the web app. Restores `_restored/` state into the pristine `openalgo/` clone first. |
| `START_OPENALGO.bat` | Double-click wrapper for the above. |
| `run_economic_engine.py` | Runs one economic pass and appends it to the run history. **Writes** to `data/economic_runs/`. |
| `run_pov.py` | Runs the POV engine (read-only analysis). |

## Verify — run these to confirm the system is healthy

| Script | Purpose |
|---|---|
| `verify_openalgo.py` | Probes 20 OpenAlgo REST endpoints. Documents the real auth contract: `apikey` goes in the **JSON body** for POST, `?apikey=` for GET. A Bearer header is silently rejected. |
| `verify_strategy_api.py` | Strategy / portfolio subsystem endpoints. |
| `verify_live_stack.py` | **The one that matters.** End-to-end check that the live macro providers actually resolve and the values are plausible. Supersedes the older per-provider verifiers. |
| `check_dbs.py` | `PRAGMA integrity_check` across all five SQLite databases. |
| `check_dash_datamode.py` | Asserts the mode banner, `body[data-mode]` and the rendered CSS palette all agree on every Dash route. |
| `smoke_test.py` | Broad smoke test of the engine package. |

## Test — offline, no network

| Script | Purpose |
|---|---|
| `test_mospi_provider.py` | 31 assertions. Pins the two `rows[0]` traps: CPI returns Rural first (the headline is Combined), and PLFS returns rural-male-2022 first. |
| `test_rbi_provider.py` | 7 assertions on the FBIL parser contract. |
| `probe_rbi_reference_rate.py` | Confirms the rbi.org.in homepage still parses for the reference rate. |
| `probe_sdk.py` | Inspects the installed OpenAlgo SDK surface. |

## Audit

| Script | Purpose |
|---|---|
| `audit_macro_values.py` | Every macro value against verified ground truth, plus a record of which sources were **ruled out and why**. Run this before trusting any macro number. |

---

## `diagnostics/`

Discovery probes used while reverse-engineering data sources. Kept because each
one records *why* a source was rejected — re-deriving that costs hours.

`diagnostics/superseded/` holds verifiers made redundant by
`verify_live_stack.py`. They still run; they are simply narrower. Kept for
reference rather than deleted, since each one contains the working endpoint
shape for a source we may revisit.

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