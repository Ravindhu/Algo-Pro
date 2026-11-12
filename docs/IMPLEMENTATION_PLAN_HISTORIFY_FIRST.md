# Implementation plan: Historify first, NSE MCP as fallback only

Date: 2026-11-12
Status: PLANNED. No code changed.
Supersedes the fetch step (Phase 1) in docs/IMPLEMENTATION_PLAN_NSE_MCP_CACHE_BACKTEST.md. The rest of that plan (QA, cached backtest, optimizer, feedback, broker-price cache) still applies.
Flow stage: Market inputs (research data), Research validation.

## Finding: Historify is already in our code

- Algo-Pro vendors OpenAlgo under src/quant/openalgo_platform/, and Historify is part of it (blueprints/historify.py, services/historify_service.py, database/historify_db.py).
- It downloads history from the connected broker into a local DuckDB store.
- Stored intervals: 1m and D. Other intervals are computed from the store with SQL.
- Export: CSV, TXT, ZIP or Parquet, via POST /historify/api/export/bulk.
- Read from the local store: source "db" (no broker limits once stored).
- Jobs run in the background, with per-symbol progress, pause, resume, cancel and retry.
- Built-in rate protection: 1 to 3 seconds between symbols, plus a 5 to 10 second cooldown every 10 symbols.
- Incremental mode fetches only the missing dates.

Open issue from the vendored docs: the DB path variable name conflicts (HISTORIFY_DATABASE_URL vs HISTORIFY_DATABASE_PATH). Pick one name in config before the first run.

## What Historify needs to run

1. OpenAlgo running on 127.0.0.1:5000.
2. A broker connected in OpenAlgo, with a valid daily login.
3. An OpenAlgo API key for the logged-in user (Historify uses it for downloads).
4. Broker history depth for the daily interval of at least 5 years. This varies by broker. Check it before anything else.

Broker history limits (from third-party OpenAlgo tutorials, not verified here):
- Zerodha: 10+ years only with the paid "Historical Chart Data" add-on, which one tutorial prices at Rs 4000 per month.
- Fyers: up to about 7 years.
- Other brokers: unknown until checked.

This is the main cost question. The cheapest route to 5 years depends on the owner's broker.

## Decision order for each symbol

1. Historify download (broker) for D, last 5 years. If the broker returns full history, use it.
2. If the broker returns less than 5 years, or Historify fails for that symbol after retries, use the NSE MCP bhavcopy for that symbol only. Record the source per row.
3. If both fail, the symbol is left out of the run and listed in the report. Never filled with zeros.

## Phase 0: owner checks (no code)

- Confirm broker name and daily-data history depth for 5 years (write the answer in the decision note).
- Confirm the broker's data terms allow personal research use.
- Record the broker as a research data source in config/data_sources.yaml: historify_broker: research_allowed: true, live_allowed: false.
- Read the NSE terms (for the MCP fallback only). Record the decision.
- Exit check: nothing is downloaded until these are recorded.

## Phase 1: Historify download job (script)

File: scripts/research/historify_download.py

- Calls the Historify routes with the OpenAlgo API key from .env, never printed or logged.
- Step a: POST /historify/api/watchlist/bulk with the screened symbols (or config/research_universe.yaml until the screener writer exists).
- Step b: POST /historify/api/jobs with job_type "watchlist", interval "D", the 5-year date range, and incremental true.
- Step c: poll GET /historify/api/jobs/<job_id> until completed or completed_with_errors. Log counts, not data.
- Step d: read per-symbol item status from the job. Failed items are listed, not retried silently beyond the built-in retry.
- Step e: export with POST /historify/api/export/bulk as Parquet, one file per symbol, into data/nse_cache/historify/.
- Writes data/nse_cache/manifest.json: symbol, source = "historify_broker", row count, first and last date, job id, export checksum, fetch time.

Exit checks:
- Unit tests with recorded fixtures. No broker calls in tests.
- A job with a failed symbol still completes the others, and the report lists the failure.
- The script refuses to run if OpenAlgo is not reachable, or if no broker is logged in. It does not fall back silently.

## Phase 2: NSE MCP fallback (only for listed gaps)

File: scripts/research/nse_mcp_fallback.py

- Runs only for symbols in the "gap" list from Phase 1, and only if research_allowed is true for NSE MCP.
- Bhavcopy server only. Live server refused by name.
- Writes to the same cache, with source = "nse_mcp_bhavcopy".
- Exit checks: a symbol that Historify already covers is never fetched from MCP (test).

## Phase 3: data QA (unchanged from the earlier plan)

Same checks as the earlier plan: zero or negative prices, High/Low/Close consistency, duplicate dates, missing days against the NSE holiday calendar, unexplained jumps, zero-volume flags. Failure stops the run.

Extra check for Historify data: compare the stored D bars with the NSE MCP bhavcopy for a sample of dates (owner sets the sample). Differences above the tolerance are reported. This also tests the broker's data quality.

## Phase 4: backtest, optimizer, feedback (unchanged)

Backtest reads only the local cache. Optimizer records every trial. Feedback report recommends only. See the earlier plan, Phases 3 to 5.

## Phase 5: broker quotes (unchanged)

Live quotes go to a separate cache, never into the historical bars. Stale quotes are ignored.

## Cost and time (planning estimate)

- Historify download: 5 symbols x 5 years of daily bars is small. The built-in delays add a few minutes per symbol. Total time depends on the broker's response time.
- Cost: depends on the broker. Zero if the broker's free history covers 5 years. Otherwise the broker add-on, or the NSE MCP fallback (free).
- Tokens: none, scripts do not call a model.

## Owner decisions

1. Which broker is connected, and does its daily history cover 5 years?
2. Pay for a broker add-on, or use the NSE MCP fallback for the gaps?
3. DB path variable name for Historify (resolve the open conflict).
4. Broker data terms: personal research use confirmed?
5. Sample size and tolerance for the Historify vs bhavcopy comparison.

## Not in this plan

- Live trading, live signals or any change to risk limits.
- Writing into Historify's live tables from research code.
- Using the NSE MCP live server.

## Repo note

This session cannot push to Ravindhu/Algo-Pro. The owner adds this file, or grants write access, before the build starts.
