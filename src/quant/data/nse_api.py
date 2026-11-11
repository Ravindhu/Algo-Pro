"""NSE public-data access layer (nsepython wrapper). Frontline inputs only.

Flow stage: Market inputs (economic, sector, screener, calendar)
Status: PHASE 3 IMPLEMENTED (2026-11-11)
Wraps: nsepython 2.101 (https://unofficed.com/nse-python/documentation/), which calls NSE's
own public REST endpoints. Nothing here re-implements an NSE endpoint.

Function names below were read from the installed package (inspect.signature), not guessed:
  nse_holidays(type='trading')            holiday_master(type='trading')
  nse_blockdeal()                         get_blockdeals() / get_bulkdeals()
  nse_annual_reports(symbol, ...)         nse_events()
  nse_fiidii(mode='pandas')               nse_get_index_quote(index)
  index_history(symbol, start_date, end_date)   nse_circular(mode='latest')
  get_beta(symbol, days=365, symbol2='NIFTY 50')

BOUNDARY (AGENTS.md rule 3)
---------------------------
This module produces INPUTS. It never builds an order, never imports an execution client,
and never writes to the risk gate. Every function returns plain data or raises NseUnavailable.

DATA CLASSIFICATION (AGENTS.md "Official Documentation and Data Sources")
-------------------------------------------------------------------------
NSE public endpoints are DELAYED / end-of-day data, not a licensed real-time feed. They are
suitable for research, screening, calendar and event inputs. They are NOT the live feed that
`config/data_sources.yaml` must approve for order generation.

FAIL CLOSED
-----------
nsepython raises on network/parse errors and NSE rate-limits aggressively. Every call is
wrapped: a failure raises NseUnavailable, never a silent partial result.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "NseUnavailable", "NseApi", "trading_holidays", "block_deals", "bulk_deals",
    "annual_reports", "event_calendar", "fii_dii_activity", "index_quote", "index_history",
    "holiday_circulars", "security_beta",
]


class NseUnavailable(RuntimeError):
    """The NSE endpoint could not be reached or returned an unusable payload."""


def _mod():
    """Import nsepython lazily: the package is optional and slow to import."""
    try:
        import nsepython  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment dependent
        raise NseUnavailable(f"nsepython not installed: {exc}") from exc
    return nsepython


def _call(fn_name: str, *args, **kwargs) -> Any:
    mod = _mod()
    fn = getattr(mod, fn_name, None)
    if fn is None:
        raise NseUnavailable(f"nsepython has no function {fn_name!r}")
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        raise NseUnavailable(f"{fn_name} failed: {exc}") from exc


def _rows(data: Any) -> List[Dict[str, Any]]:
    """Normalise a payload to a list of dict rows (pandas DataFrame or list)."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if hasattr(data, "to_dict"):  # pandas DataFrame
        try:
            return [r for r in data.to_dict("records") if isinstance(r, dict)]
        except Exception:
            return []
    return []


# --- calendar -------------------------------------------------------------------
def trading_holidays(year: Optional[int] = None) -> List[Dict[str, Any]]:
    """NSE trading holidays (nse_holidays(type='trading'))."""
    rows = _rows(_call("nse_holidays", "trading"))
    if year is None:
        return rows
    return [r for r in rows if str(year) in " ".join(str(v) for v in r.values())]


def holiday_circulars() -> List[Dict[str, Any]]:
    """NSE circulars (nse_circular(mode='latest')). Filter for holiday notices by the caller."""
    return _rows(_call("nse_circular", "latest"))


# --- deals ----------------------------------------------------------------------
def block_deals() -> List[Dict[str, Any]]:
    """Block deals (nse_blockdeal)."""
    return _rows(_call("nse_blockdeal"))


def bulk_deals() -> List[Dict[str, Any]]:
    """Bulk deals (get_bulkdeals; nsepython has no nse_bulkdeal)."""
    return _rows(_call("get_bulkdeals"))


# --- filings and events ----------------------------------------------------------
def annual_reports(symbol: str, year_from: Optional[int] = None,
                   year_to: Optional[int] = None) -> List[Dict[str, Any]]:
    """Annual reports for one symbol (nse_annual_reports)."""
    return _rows(_call("nse_annual_reports", symbol, year_from, year_to))


def event_calendar() -> List[Dict[str, Any]]:
    """Corporate event calendar (nse_events)."""
    return _rows(_call("nse_events"))


# --- flows ------------------------------------------------------------------------
def fii_dii_activity() -> Any:
    """FII/DII activity (nse_fiidii). Returns the payload as nsepython gives it."""
    return _call("nse_fiidii", "pandas")


# --- index -------------------------------------------------------------------------
def index_quote(index: str = "NIFTY 50") -> Dict[str, Any]:
    """Index quote (nse_get_index_quote). Delayed, not a licensed feed."""
    data = _call("nse_get_index_quote", index)
    return data if isinstance(data, dict) else {"data": data}


def index_history(index: str = "NIFTY 50", start: Optional[date] = None,
                  end: Optional[date] = None):
    """Index OHLC history (index_history). Dates are dd-MMM-yyyy strings."""
    fmt = "%d-%b-%Y"
    end = end or date.today()
    start = start or end
    return _call("index_history", index, start.strftime(fmt), end.strftime(fmt))


# --- risk inputs ---------------------------------------------------------------------
def security_beta(symbol: str, days: int = 365, index: str = "NIFTY 50") -> Optional[float]:
    """Beta of a security vs an index (get_beta). None when the value is unusable. Never guesses."""
    try:
        val = _call("get_beta", symbol, days, index)
    except NseUnavailable:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, dict):
        for key in ("beta", "Beta", "betaValue"):
            v = val.get(key)
            if isinstance(v, (int, float)):
                return float(v)
    return None


class NseApi:
    """Thin object wrapper so callers can inject a fake in tests."""

    def __init__(self, fetch: Optional[Callable[[str, tuple, dict], Any]] = None) -> None:
        self._fetch = fetch

    def _get(self, name: str, *args, **kwargs):
        if self._fetch is not None:
            return self._fetch(name, args, kwargs)
        return _call(name, *args, **kwargs)

    def holidays(self) -> List[Dict[str, Any]]:
        return _rows(self._get("nse_holidays", "trading"))

    def block_deals(self) -> List[Dict[str, Any]]:
        return _rows(self._get("nse_blockdeal"))

    def bulk_deals(self) -> List[Dict[str, Any]]:
        return _rows(self._get("get_bulkdeals"))

    def events(self) -> List[Dict[str, Any]]:
        return _rows(self._get("nse_events"))

    def fii_dii(self) -> Any:
        return self._get("nse_fiidii", "pandas")

    def index_quote(self, index: str = "NIFTY 50") -> Dict[str, Any]:
        data = self._get("nse_get_index_quote", index)
        return data if isinstance(data, dict) else {"data": data}
