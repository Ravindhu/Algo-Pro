"""Optional NSE access layer: nse-mcp when installed, direct NSE JSON otherwise.

Flow stage: India flow data, Bulk and block deals, Relative strength
Status: PHASE 2 IMPLEMENTED (2026-11-14)
Existing code to reuse or wrap (do not copy):
    nse-mcp   (``block_deal_session``, ``all_indices``, ``index_history``)
    jugaad-data (``NSELive.trade_info`` -> bulkBlockDeals, ``index_df``)
    src/quant/blue_engine/economics/providers/fii_provider.py (the browser-UA
        rule and the JSON parse already proven against NSE)

WHY THIS FILE EXISTS
--------------------
The user asked for data through nse-mcp. That package is NOT installed here and
must not be installed on this machine, so this module is an ADAPTER, not a
dependency:

* It imports ``nse_mcp`` / ``jugaad_data`` LAZILY, inside functions.
* Nothing at module import time requires either package.
* When neither is present it falls back to NSE's own JSON endpoints, using the
  same request shape that already works in ``data/nse_fii.py``.
* Every source reports which one actually answered, so a result is never
  mistaken for coming from a source it did not come from.

Coverage, stated honestly (verified against the published nse-mcp README):

| Need            | nse-mcp tool        | jugaad-data                    | direct NSE JSON |
|-----------------|---------------------|--------------------------------|-----------------|
| Block deals     | ``block_deal_session`` | ``NSELive.trade_info``      | session endpoint |
| Bulk deals      | **none**            | ``NSELive.trade_info`` (bulkBlockDeals) | session endpoint |
| FII/DII flows   | **none**            | —                              | ``fiidiiTradeReact`` |
| Index levels    | ``all_indices``     | ``index_df``                  | ``allIndices`` |
| Sector RS       | ``all_indices``     | ``index_df``                  | ``allIndices`` |

nse-mcp does NOT provide bulk deals or FII/DII. Any design that assumes it does
would silently produce empty data, which is why bulk deals is handled by
jugaad-data's ``trade_info`` or the direct endpoint instead.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "SOURCES",
    "available_sources",
    "best_source",
    "fetch_block_deals",
    "fetch_bulk_deals",
    "fetch_index_levels",
    "fetch_index_history",
    "fetch_fii_dii",
]

#: Ordered preference. The first AVAILABLE source wins; the caller is told which.
SOURCES = ("nse_mcp", "jugaad_data", "nse_json")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

NSE_BLOCK_DEAL_URL = "https://www.nseindia.com/api/block-deal"
NSE_BULK_DEAL_URL = "https://www.nseindia.com/api/bulk-deal"
NSE_ALL_INDICES_URL = "https://www.nseindia.com/api/allIndices"
NSE_FII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"


# --------------------------------------------------------------------------- #
# source detection
# --------------------------------------------------------------------------- #
def _has(module: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def available_sources() -> List[str]:
    """Which sources can actually be used here, in preference order.

    Detected by import spec only: nothing is imported and nothing is installed.
    """
    out: List[str] = []
    if _has("nse_mcp"):
        out.append("nse_mcp")
    if _has("jugaad_data"):
        out.append("jugaad_data")
    out.append("nse_json")  # always possible if requests is installed
    return out


def best_source(prefer: Optional[str] = None) -> str:
    """The source that will be used. ``prefer`` is honoured only if available."""
    avail = available_sources()
    if prefer and prefer in avail:
        return prefer
    return avail[0]


# --------------------------------------------------------------------------- #
# direct NSE JSON (always available; the fallback)
# --------------------------------------------------------------------------- #
def _json_get(url: str, timeout: float, http_get) -> Dict[str, Any]:
    """Shared GET with NSE's header rules. Returns a result dict, never raises."""
    getter = http_get
    if getter is None:
        try:
            import requests
            getter = requests.get
        except ImportError:
            return {"ok": False, "payload": None, "status": None,
                    "detail": "requests is not installed"}
    headers = {"User-Agent": _UA,
               "Accept": "application/json, text/plain, */*",
               "Accept-Language": "en-US,en;q=0.9",
               "Referer": "https://www.nseindia.com/"}
    try:
        resp = getter(url, timeout=timeout, headers=headers)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "payload": None, "status": None,
                "detail": f"request failed: {exc!r}"}
    status = getattr(resp, "status_code", None)
    if status is not None and status != 200:
        return {"ok": False, "payload": None, "status": status,
                "detail": f"HTTP {status}; NSE needs a warm session cookie"}
    try:
        return {"ok": True, "payload": resp.json() if hasattr(resp, "json") else resp,
                "status": status, "detail": ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "payload": None, "status": status,
                "detail": f"response is not JSON: {exc!r}"}


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _screened_symbols(path=None) -> List[str]:
    """Symbols currently in the screener output. Empty when there is no screen.

    Deals are collected for the SCREENED book only, so this universe is the
    filter. A missing or unreadable screen returns [] rather than every symbol:
    fetching deals for a universe we cannot verify would waste calls and store
    rows nobody asked for.
    """
    import json
    from pathlib import Path

    screen_path = Path(path) if path else (
        Path(__file__).resolve().parents[3] / "data" / "screened" / "latest.json")
    if not screen_path.is_file():
        logger.info("no screener output at %s; screened universe is empty", screen_path)
        return []
    try:
        payload = json.loads(screen_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("screener output unreadable: %r", exc)
        return []

    # The writer stores {"as_of": ..., "symbols": [...]}; a bare list is also
    # accepted so a hand-written file still works.
    rows = payload.get("symbols") if isinstance(payload, dict) else payload
    out: List[str] = []
    for row in rows or []:
        sym = (row if isinstance(row, str) else (row or {}).get("symbol"))
        sym = str(sym or "").strip().upper()
        if sym and sym not in out:
            out.append(sym)
    return out


def _rows_from_deal_payload(payload: Any, kind: str,
                            trade_date: Optional[date]) -> List[Dict[str, Any]]:
    """Normalise a block/bulk deal payload into store rows.

    Accepts the ``{"data": [...]}`` shape NSE uses and a bare list. A row without
    a symbol or a quantity is skipped: a deal with no size is not a deal.
    """
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("records") or []
    if not isinstance(payload, (list, tuple)):
        return []
    day = (trade_date or date.today()).isoformat()
    out: List[Dict[str, Any]] = []
    for rec in payload:
        if not isinstance(rec, dict):
            continue
        symbol = str(rec.get("symbol") or rec.get("Symbol") or "").strip().upper()
        qty = _num(rec.get("quantity") or rec.get("Quantity")
                   or rec.get("qty") or rec.get("tradedQuantity"))
        if not symbol or qty is None:
            continue
        out.append({
            "trade_date": day,
            "deal_type": kind,
            "symbol": symbol,
            "client_name": str(rec.get("clientName") or rec.get("client") or "").strip(),
            "buy_sell": str(rec.get("buySell") or rec.get("dealType") or "").strip().upper(),
            "quantity": qty,
            "price": _num(rec.get("price") or rec.get("tradePrice") or rec.get("wap")),
            "value_inr": _num(rec.get("value") or rec.get("tradedValue")),
        })
    return out


# --------------------------------------------------------------------------- #
# block deals
# --------------------------------------------------------------------------- #
def fetch_block_deals(*, trade_date: Optional[date] = None, timeout: float = 15.0,
                      http_get=None, prefer: Optional[str] = None) -> Dict[str, Any]:
    """Block deals for a session.

    Order: nse-mcp ``block_deal_session`` -> jugaad-data ``trade_info`` ->
    direct NSE JSON. Returns ``{"ok", "rows", "source", "detail"}``. Never raises.
    """
    source = best_source(prefer)

    if source == "nse_mcp":
        try:
            from nse_mcp import block_deal_session  # type: ignore
            payload = block_deal_session()
            rows = _rows_from_deal_payload(payload, "block", trade_date)
            if rows:
                return {"ok": True, "rows": rows, "source": "nse_mcp", "detail": ""}
            logger.warning("nse_mcp block_deal_session returned no usable rows; falling back")
        except Exception as exc:  # noqa: BLE001
            logger.warning("nse_mcp block_deal_session failed: %r", exc)

    if source in ("nse_mcp", "jugaad_data") and _has("jugaad_data"):
        try:
            from jugaad_data.nse import NSELive  # type: ignore
            live = NSELive()
            # Verified against jugaad-data 0.37.1: the method is
            # ``block_deal_session()``. There is no ``block_deal()``.
            payload = live.block_deal_session() if hasattr(live, "block_deal_session") else None
            rows = _rows_from_deal_payload(payload, "block", trade_date)
            if rows:
                return {"ok": True, "rows": rows, "source": "jugaad_data", "detail": ""}
        except Exception as exc:  # noqa: BLE001
            logger.warning("jugaad-data block deals failed: %r", exc)

    got = _json_get(NSE_BLOCK_DEAL_URL, timeout, http_get)
    if not got["ok"]:
        return {"ok": False, "rows": [], "source": "nse_json", "detail": got["detail"]}
    rows = _rows_from_deal_payload(got["payload"], "block", trade_date)
    return {"ok": bool(rows), "rows": rows, "source": "nse_json",
            "detail": "" if rows else "payload parsed to zero rows"}


# --------------------------------------------------------------------------- #
# bulk deals
# --------------------------------------------------------------------------- #
def fetch_bulk_deals(*, trade_date: Optional[date] = None, timeout: float = 15.0,
                     http_get=None, prefer: Optional[str] = None) -> Dict[str, Any]:
    """Bulk deals for a session.

    nse-mcp has NO bulk-deal tool (verified against its README), so the order is
    jugaad-data ``trade_info`` -> direct NSE JSON. Attempting nse-mcp first would
    only waste a call on an AttributeError.
    """
    source = best_source(prefer)

    if source in ("nse_mcp", "jugaad_data") and _has("jugaad_data"):
        try:
            from jugaad_data.nse import NSELive  # type: ignore
            live = NSELive()
            # Verified against jugaad-data 0.37.1: there is NO bulk-deal method.
            # Bulk deals come from ``trade_info(symbol)`` -> bulkBlockDeals, which
            # is PER SYMBOL, so this needs a symbol list. Without one we go to the
            # session endpoint rather than guessing a symbol.
            symbols = _screened_symbols()
            rows: List[Dict[str, Any]] = []
            for sym in symbols:
                try:
                    info = live.trade_info(sym) or {}
                except Exception:  # noqa: BLE001 - one bad symbol must not stop the rest
                    continue
                deals = info.get("bulkBlockDeals") or {}
                for entry in (deals.get("data") or []):
                    rows.extend(_rows_from_deal_payload([entry], "bulk", trade_date))
                if rows:
                    break
            if rows:
                return {"ok": True, "rows": rows, "source": "jugaad_data", "detail": ""}
        except Exception as exc:  # noqa: BLE001
            logger.warning("jugaad-data bulk deals failed: %r", exc)

    got = _json_get(NSE_BULK_DEAL_URL, timeout, http_get)
    if not got["ok"]:
        return {"ok": False, "rows": [], "source": "nse_json", "detail": got["detail"]}
    rows = _rows_from_deal_payload(got["payload"], "bulk", trade_date)
    return {"ok": bool(rows), "rows": rows, "source": "nse_json",
            "detail": "" if rows else "payload parsed to zero rows"}


# --------------------------------------------------------------------------- #
# index levels (sector RS input)
# --------------------------------------------------------------------------- #
def fetch_index_levels(*, trade_date: Optional[date] = None, timeout: float = 15.0,
                       http_get=None, prefer: Optional[str] = None) -> Dict[str, Any]:
    """Current level of every NSE index. nse-mcp ``all_indices`` first."""
    source = best_source(prefer)

    if source == "nse_mcp":
        try:
            from nse_mcp import all_indices  # type: ignore
            payload = all_indices()
            rows = _index_rows(payload, trade_date)
            if rows:
                return {"ok": True, "rows": rows, "source": "nse_mcp", "detail": ""}
        except Exception as exc:  # noqa: BLE001
            logger.warning("nse_mcp all_indices failed: %r", exc)

    got = _json_get(NSE_ALL_INDICES_URL, timeout, http_get)
    if not got["ok"]:
        return {"ok": False, "rows": [], "source": "nse_json", "detail": got["detail"]}
    rows = _index_rows(got["payload"], trade_date)
    return {"ok": bool(rows), "rows": rows, "source": "nse_json",
            "detail": "" if rows else "payload parsed to zero index rows"}


def _index_rows(payload: Any, trade_date: Optional[date]) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("data") or payload.get("records") or []
    if not isinstance(payload, (list, tuple)):
        return []
    day = (trade_date or date.today()).isoformat()
    from quant.data.nse_sectors import parse_all_indices
    return parse_all_indices(payload, trade_date=date.fromisoformat(day))


def fetch_index_history(index_name: str, start: date, end: date, *,
                        prefer: Optional[str] = None) -> Dict[str, Any]:
    """Historical index levels. nse-mcp ``index_history`` -> jugaad ``index_df``.

    Returns ``{"ok", "rows", "source", "detail"}`` where each row is
    ``{"trade_date", "index_name", "last"}``.
    """
    source = best_source(prefer)

    if source == "nse_mcp":
        try:
            from nse_mcp import index_history  # type: ignore
            payload = index_history(index_name, start.isoformat(), end.isoformat())
            rows = _history_rows(payload, index_name)
            if rows:
                return {"ok": True, "rows": rows, "source": "nse_mcp", "detail": ""}
        except Exception as exc:  # noqa: BLE001
            logger.warning("nse_mcp index_history failed: %r", exc)

    if _has("jugaad_data"):
        try:
            from jugaad_data.nse import index_df  # type: ignore
            df = index_df(index_name, start, end)
            rows = _history_rows(df, index_name)
            if rows:
                return {"ok": True, "rows": rows, "source": "jugaad_data", "detail": ""}
        except Exception as exc:  # noqa: BLE001
            logger.warning("jugaad-data index_df failed: %r", exc)

    return {"ok": False, "rows": [], "source": "none",
            "detail": "no index-history source available (install jugaad-data or use the weekly store)"}


def _history_rows(payload: Any, index_name: str) -> List[Dict[str, Any]]:
    """Accept a DataFrame (jugaad) or a list of dicts (MCP)."""
    rows: List[Dict[str, Any]] = []
    records = payload
    if hasattr(payload, "to_dict"):  # pandas DataFrame
        try:
            records = payload.to_dict("records")
        except Exception:  # noqa: BLE001
            return []
    if not isinstance(records, (list, tuple)):
        return []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        day = rec.get("HistoricalDate") or rec.get("date") or rec.get("trade_date")
        close = _num(rec.get("CLOSE") or rec.get("close") or rec.get("last"))
        if day is None or close is None:
            continue
        text = str(day)[:10]
        try:
            date.fromisoformat(text)
        except ValueError:
            continue
        rows.append({"trade_date": text, "index_name": index_name.upper(), "last": close})
    return rows


# --------------------------------------------------------------------------- #
# FII / DII
# --------------------------------------------------------------------------- #
def fetch_fii_dii(*, timeout: float = 15.0, http_get=None,
                  prefer: Optional[str] = None) -> Dict[str, Any]:
    """FII/DII provisional flows. nse-mcp has NO tool for this, so it is NSE JSON.

    Kept here so every call site asks this module rather than knowing which
    source covers what.
    """
    got = _json_get(NSE_FII_URL, timeout, http_get)
    if not got["ok"]:
        return {"ok": False, "rows": [], "source": "nse_json", "detail": got["detail"]}
    from quant.data.nse_fii import parse_fii_dii
    rows = parse_fii_dii(got["payload"])
    return {"ok": bool(rows), "rows": rows, "source": "nse_json",
            "detail": "" if rows else "payload parsed to zero rows"}
