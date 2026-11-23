"""market - Market engine package.

Responsibilities:
    * market_engine.py        : live market state, depth, time-series cache.
    * sqlite_downloader.py    : bulk NSE historical tick downloader (OpenAlgo
                                history API -> local SQLite/Parquet).
    * ieod.py                 : intraday end-of-day CSV downloader (per-symbol).
    * symbols/                : symbol universe files (one CSV per index/exchange).
    * .env.example            : downloader-specific environment variables
                                (API_KEY, HOST, INTERVAL, RATE LIMITS, etc.).

All files in this directory are part of the data acquisition layer; they
never place orders. Execution lives one layer up in `Blue_engine/execution/`.
"""
from .market_engine import MarketEngine

__all__ = ["MarketEngine"]
