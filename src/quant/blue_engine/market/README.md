# Blue_engine/market — Market Data Layer

This directory owns **all data acquisition** for the trading engine. It is the
only place in `Blue_engine/` that talks to OpenAlgo's `history` endpoint.

## Contents

| File / Dir | Purpose |
| --- | --- |
| `market_engine.py` | Live market state, depth cache, in-memory time-series. Used by strategy logic. |
| `sqlite_downloader.py` | Bulk NSE historical tick downloader. Streams symbols from `symbols/*.csv` into `data/openalgo_db/<DB_NAME>.db` via OpenAlgo's history API. Resumable via `checkpoints.json`. |
| `ieod.py` | Intraday end-of-day CSV downloader. One CSV per symbol under `downloads/`. Interactive mode picker (today / 5d / 30d / 90d / 1y / 2y / 5y / 10y). |
| `symbols/` | Symbol universe files (one CSV per index/exchange). Currently `sample_nse.csv` (6 large-cap names) for smoke tests. |
| `.env.example` | Downloader-specific environment variables. Copy to `.env` and fill in real values. |
| `downloads/` | Runtime output of `ieod.py` (gitignored). |
| `data_download.log` | Runtime log of `ieod.py` (gitignored). |

## Rules

* **No order placement.** This directory only *reads* market data.
  Execution lives at `Blue_engine/execution/`.
* **No direct broker calls.** All history fetches go through the OpenAlgo
  client (`from openalgo import api`) so the broker can be swapped without
  touching the downloaders.
* **Symbols are data, not code.** New universes go into `symbols/<name>.csv`
  so the same list can be reused across backtests, downloads, and live runs.

## Quick start

```bash
cd Blue_engine/market
cp .env.example .env       # then fill in OPENALGO_API_KEY
python sqlite_downloader.py    # bulk historical into data/openalgo_db/
python ieod.py                 # interactive intraday CSV dump
```
