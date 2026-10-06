# Symbol Universe Files

One CSV file per index / exchange / strategy. Each CSV is a single column of
trading symbols with **no header**, e.g.

```
RELIANCE
ICICIBANK
HDFCBANK
SBIN
TCS
INFY
```

Files in this directory:

| File | Purpose |
| --- | --- |
| `sample_nse.csv` | Six-symbol NSE sample set used by the downloaders' smoke tests and the dashboard's "quick load" preset. |

## Adding a new universe

1. Create `<name>.csv` with one symbol per line, no header, LF line endings.
2. Reference it from your strategy / notebook / dashboard as
   `Blue_engine.market.symbols.<name>`.
3. Do **not** add per-symbol tickers one-off in code; keep them in data files
   so the same universe can be reused across backtests, downloads, and live
   runs.

## Provenance

`sample_nse.csv` was restored from the previous-laptop local checkout. It is
the smallest symbol set that exercises the full downloader pipeline
(mix of large-cap banking, energy, and IT names) without triggering OpenAlgo
rate limits during a smoke test.
