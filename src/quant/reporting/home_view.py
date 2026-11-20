"""Home-page data: screener list and portfolio analysis (TWRR, Sharpe, drawdown).

Read-only. Nothing here sends orders or writes files.

* Screener list: the Blue Engine screener's last saved run
  (``blue_engine/screener/last_screen.json``, written by its CLI with ``--save``).
* Portfolio analysis (REAL money): the Blue Engine NAV store, built from the broker's view of the live book, read through
  ``blue_engine.portfolio.risk_report.report``, the same code the desk uses.

A missing file, too few NAV rows, or a Blue Engine import error gives an empty
state with a short note. It never raises into the dashboard poll.

Flow stage: Dashboard
Status: PHASE 3 IMPLEMENTED (read-only; no order path)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[3]
SCREEN_PATH = ROOT / "src" / "quant" / "blue_engine" / "screener" / "last_screen.json"
MAX_ROWS = 50        # AGENTS.md token rules: cap what reaches the page
CACHE_S = 60.0       # the NAV store changes once a day; no need to re-read every poll

_analysis_cache: Dict[str, Any] = {"at": None, "value": None}


def read_screener(path: Path = SCREEN_PATH) -> Dict[str, Any]:
    """The selected names from the last screener run, or an empty state."""
    if not path.is_file():
        return {"state": "not_run", "as_of": None, "source": None, "count": 0, "rows": [],
                "note": "No screener run yet. Run the Blue Engine screener with --save."}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "error", "as_of": None, "source": None, "count": 0, "rows": [],
                "note": "Screener file could not be read."}
    results = payload.get("results") if isinstance(payload, dict) else None
    results = results if isinstance(results, list) else []
    selected = [r for r in results if isinstance(r, dict) and r.get("selected")]
    rows: List[Dict[str, str]] = [{
        "symbol": str(r.get("symbol") or "")[:20],
        "name": str(r.get("name") or "")[:40],
        "status": "selected",
        "reason": str(r.get("reason") or "")[:120],
    } for r in selected[:MAX_ROWS]]
    return {"state": "ok" if rows else "empty",
            "as_of": payload.get("as_of") if isinstance(payload, dict) else None,
            "source": str(payload.get("source") or "")[:120] if isinstance(payload, dict) else None,
            "count": len(selected), "rows": rows,
            "note": "" if rows else "The last screener run selected no names."}


def _pct(value: Any) -> Optional[float]:
    """Fraction to percent, rounded; None stays None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value) * 100.0, 2)


def _ensure_blue_engine_path() -> None:
    """The Blue Engine imports its own modules as top-level ``blue_engine``, so src/quant must be on sys.path."""
    import sys
    entry = str(ROOT / "src" / "quant")
    if entry not in sys.path:
        sys.path.append(entry)


def _load_nav_rows() -> List[Dict[str, Any]]:
    _ensure_blue_engine_path()
    from quant.blue_engine.portfolio import tracker  # imported here: heavy, and optional at runtime
    return tracker.read_nav(months=12)


def portfolio_analysis(*, now: Optional[float] = None,
                       rows_fn: Callable[[], List[Dict[str, Any]]] = _load_nav_rows) -> Dict[str, Any]:
    """TWRR, annualised return, Sharpe and max drawdown from the Blue Engine NAV store (cached 60 s)."""
    clock = time.time() if now is None else now
    if _analysis_cache["at"] is not None and clock - _analysis_cache["at"] < CACHE_S:
        return _analysis_cache["value"]
    value = _compute_analysis(rows_fn)
    _analysis_cache.update(at=clock, value=value)
    return value


def _compute_analysis(rows_fn: Callable[[], List[Dict[str, Any]]]) -> Dict[str, Any]:
    try:
        _ensure_blue_engine_path()
        from quant.blue_engine.portfolio.risk_report import report
    except Exception as exc:  # name the error type only
        return {"state": "unavailable", "note": f"Blue Engine not importable ({type(exc).__name__})"}
    try:
        rows = rows_fn()
    except Exception as exc:
        return {"state": "unavailable", "note": f"NAV store not readable ({type(exc).__name__})"}
    path = [{"date": str(r.get("date"))[:10], "nav": float(r["nav"])} for r in rows
            if isinstance(r.get("nav"), (int, float)) and not isinstance(r.get("nav"), bool)][-260:]
    if len(rows) < 2:
        return {"state": "needs_nav", "nav_rows": len(rows), "nav_path": path,
                "note": "Needs at least two daily NAV rows from the Blue Engine portfolio."}
    try:
        rep = report(rows)
    except Exception as exc:
        return {"state": "unavailable", "note": f"Analysis failed ({type(exc).__name__})"}
    summary = (rep.get("performance") or {}).get("summary") or {}
    return {
        "state": "ok",
        "nav_rows": len(rows),
        "days": rep.get("n_returns"),
        "twrr_pct": _pct(summary.get("twrr")),
        "annual_return_pct": _pct(summary.get("annual_return")),
        "sharpe": summary.get("sharpe") if isinstance(summary.get("sharpe"), (int, float)) else None,
        "max_drawdown_pct": _pct(summary.get("max_drawdown")),
        "nav_path": path,
        "note": "",
    }
