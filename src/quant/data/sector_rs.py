"""Sector relative strength: Mansfield RS scoring. Wraps the engine's scorer.

Flow stage: Relative strength, Sector engine
Status: PHASE 2 IMPLEMENTED (wrap; measurement only, advises never orders)
Existing code to reuse or wrap (do not copy):
    src/quant/blue_engine/sectors/sector_hm_score.py
    (SectorHMScorer, score_sectors - the Mansfield relative-strength model)
    src/quant/blue_engine/sectors/sector_universe_filter.py (universe filter)

WHY THE LOCAL VERSION WAS NOT ENOUGH
------------------------------------
`market_inputs.relative_strength(stock, index, window)` computes
``stock_return - index_return``. That answers "did this stock beat the index",
which is a fact, not a signal. It cannot answer:

  * which SECTOR is strong, only whether one stock beat whatever index you handed it;
  * strength relative to a moving average of relative strength (Mansfield RS),
    which is what actually distinguishes a trend from a single good week;
  * where a stock sits inside its sector, which is the question a sector-rotation
    book needs.

The engine's HM scorer does all three. This wrapper exposes it without pulling
in the full ``SectorEngine`` (which needs a market-data provider and a news feed).

SIDE-EFFECT CONTROL
-------------------
The engine's ``score_sectors`` PERSISTS its table to disk by default. A research
call must not write files, so ``persist`` is forced False here and the caller
cannot override it. Persisting is a deliberate, separate action.

WHAT IT DOES NOT DO
-------------------
- No network, no market-data provider. You pass the sector price matrix in.
- Never places or sizes anything. A sector rank is advice; ``risk/gate.py``
  remains the only path to an order.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

logger = logging.getLogger(__name__)

__all__ = [
    "SectorRSUnavailable",
    "score_sectors",
    "rank_sectors",
    "sector_of_symbol",
    "relative_strength_series",
]


class SectorRSUnavailable(RuntimeError):
    """The engine's sector scorer cannot be loaded."""


def _module():
    try:
        return load("sectors.sector_hm_score")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise SectorRSUnavailable(f"engine sector scorer unavailable: {exc}") from exc


def score_sectors(config: Optional[Dict[str, Any]] = None,
                  as_of: Optional[date] = None) -> Dict[str, Any]:
    """Sector RS table as of a date.

    ``persist`` is forced False: a research read must not write to disk.
    Returns the engine table, or ``{"status": "unavailable", ...}`` when the
    scorer cannot load, so a panel renders instead of crashing.
    """
    try:
        mod = _module()
    except SectorRSUnavailable as exc:
        return {"status": "unavailable", "detail": str(exc), "sectors": []}
    try:
        return mod.score_sectors(config, as_of, persist=False)
    except Exception as exc:  # noqa: BLE001 - a panel must render, not crash
        logger.warning("score_sectors failed: %r", exc)
        return {"status": "error", "detail": repr(exc), "sectors": []}


def rank_sectors(table: Optional[Dict[str, Any]] = None, *,
                 config: Optional[Dict[str, Any]] = None,
                 as_of: Optional[date] = None) -> List[Dict[str, Any]]:
    """Sectors strongest first, as ``[{sector, score, rank}]``.

    Sorting is done here rather than trusted from the engine payload, so a
    schema change cannot silently return an unordered list to the allocator.
    """
    data = table if table is not None else score_sectors(config, as_of)
    if not isinstance(data, dict):
        return []
    # The engine's table uses "ranked"; "sectors" is accepted as a fallback so a
    # future schema rename degrades instead of silently returning nothing.
    sectors = data.get("ranked") or data.get("sectors") or []
    if not sectors:
        return []

    def _score(entry: Dict[str, Any]) -> float:
        for key in ("score", "rs_score", "hm_score", "value", "hm"):
            value = entry.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        return float("-inf")

    ordered = sorted(sectors, key=_score, reverse=True)
    out: List[Dict[str, Any]] = []
    for i, entry in enumerate(ordered, start=1):
        name = entry.get("sector") or entry.get("name") or entry.get("index")
        out.append({"sector": name, "score": _score(entry), "rank": i})
    return out


def sector_of_symbol(symbol: str) -> Optional[str]:
    """The sector a symbol belongs to, via the engine's screener sector map.

    Returns None when the map is unavailable or the symbol is unmapped. An
    unmapped symbol is NOT guessed into a sector.
    """
    try:
        mod = load("sectors.sector_screener_map")
    except BlueEngineUnavailable:
        return None
    mapping = getattr(mod, "INDEX_TO_SCREENER_SECTORS", None)
    if not isinstance(mapping, dict):
        return None
    target = str(symbol or "").strip().upper()
    for index_name, members in mapping.items():
        if isinstance(members, (list, tuple, set)):
            if target in {str(m).strip().upper() for m in members}:
                return str(index_name)
        elif isinstance(members, str) and members.strip().upper() == target:
            return str(index_name)
    return None


def relative_strength_series(stock_close, index_close, window: int = 63):
    """Stock-minus-index return over ``window`` bars. Past bars only.

    Kept here so callers have one import for sector strength. This is the simple
    fact, not the Mansfield model; use ``score_sectors`` for the ranked signal.
    """
    if window < 1:
        raise ValueError("window must be >= 1")
    s = stock_close / stock_close.shift(window) - 1
    i = index_close / index_close.shift(window) - 1
    return s - i


def sector_rs_from_store(weeks: int = 4, *, path=None) -> Dict[str, Any]:
    """Sector relative strength computed from the WEEKLY stored index levels.

    This is the bridge between ``data/nse_sectors.py`` (which persists NSE index
    snapshots every week) and the RS question the allocator asks: which sectors
    are outperforming the broad index.

    Computed as the ``weeks``-period return of each sector index minus the same
    return of the benchmark (NIFTY 50), which is the Mansfield RS numerator.
    Past data only: every level used was stored BEFORE the query date.

    Returns::

        {"status": "ok"|"insufficient_history"|"no_data",
         "benchmark": "NIFTY 50", "as_of": str|None,
         "sectors": [{"sector", "return", "relative", "rank"}], "detail": str}

    Insufficient history is a REPORTED status, never a made-up score.
    """
    from quant.data import nse_sectors

    store = nse_sectors.SectorStore(path)
    names = store.index_names()
    if not names:
        return {"status": "no_data", "benchmark": "NIFTY 50", "as_of": None,
                "sectors": [],
                "detail": "no index levels stored; run scripts/ops/fetch_sectors_weekly.py"}

    benchmark = "NIFTY 50"
    bench_series = store.weekly_series(benchmark)
    if len(bench_series) < weeks + 1:
        return {"status": "insufficient_history", "benchmark": benchmark,
                "as_of": None, "sectors": [],
                "detail": f"benchmark has {len(bench_series)} week(s); need {weeks + 1}"}
    bench_now = bench_series[-1]["last"]
    bench_then = bench_series[-(weeks + 1)]["last"]
    if not bench_then:
        return {"status": "insufficient_history", "benchmark": benchmark,
                "as_of": None, "sectors": [],
                "detail": "benchmark start level is missing"}
    bench_ret = bench_now / bench_then - 1.0

    rows: List[Dict[str, Any]] = []
    for name in names:
        if name == benchmark:
            continue
        series = store.weekly_series(name)
        if len(series) < weeks + 1:
            continue
        last, then = series[-1]["last"], series[-(weeks + 1)]["last"]
        if not last or not then:
            continue
        ret = last / then - 1.0
        rows.append({"sector": name, "return": round(ret, 6),
                     "relative": round(ret - bench_ret, 6)})

    if not rows:
        return {"status": "insufficient_history", "benchmark": benchmark,
                "as_of": None, "sectors": [],
                "detail": f"no sector index has {weeks + 1} weeks of levels"}

    rows.sort(key=lambda r: r["relative"], reverse=True)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i

    return {"status": "ok", "benchmark": benchmark,
            "as_of": bench_series[-1]["trade_date"],
            "sectors": rows,
            "detail": f"{len(rows)} sector(s) ranked over {weeks} week(s)"}
