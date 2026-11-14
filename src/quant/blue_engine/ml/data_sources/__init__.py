"""
ml/data_sources — one module per vendor. The ONLY place in the ML layer that
touches the network or a vendor file.

WHY ONE FILE PER SOURCE (and not one big `loaders.py`)
------------------------------------------------------
Every source has a *different* failure mode, a different timestamp convention and
a different licence. Merging them into one file is how you end up with a
`try/except: pass` that hides a source silently returning zeros. One file per
source means:

  * a failure is attributable ("nse_bhavcopy is down", not "data is broken"),
  * the as_of_ts convention is documented next to the parsing that decides it,
  * a source can be deleted without touching anything else — the "plug and
    unplug" requirement in AGENTS.md, applied to data.

SOURCES SHIPPED HERE
--------------------
price_cache       Blue_engine's own accumulating OHLCV SQLite cache (offline, always available)
yfinance_source   long-history daily bars, key-free (backfill / research only)
openalgo_source   broker-fed historical bars via the OpenAlgo SDK (the traded feed)
nse_bhavcopy      official NSE end-of-day bhavcopy files (equity + F&O)
macro_source      the existing economics providers (MoSPI / RBI / FII) re-exposed
news_source       timestamped headlines/filings for the NLP + LLM models

ADDING ONE
----------
1. copy `_template.py` next to the others,
2. set `name`, `kind`, `required_columns`,
3. implement `_fetch` and stamp `as_of_ts` on every row,
4. register it in `registry.py`,
5. pin one known value in `Blue_engine/tests/test_ml_data_sources.py`.
"""

from __future__ import annotations

from .base import DataSource, FetchResult, FileDataSource, rows_hash, schema_fingerprint
from .registry import SOURCES, build_source, available_sources, describe_all

__all__ = [
    "DataSource", "FileDataSource", "FetchResult", "rows_hash",
    "schema_fingerprint", "SOURCES", "build_source", "available_sources",
    "describe_all",
]