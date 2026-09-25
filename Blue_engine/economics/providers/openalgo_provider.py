"""
providers/openalgo_provider.py - macro signals derived from the OpenAlgo SDK.

This is the *zero-extra-credential* provider. It uses only the public SDK
client (``from openalgo import api``) that the platform already authenticates,
so it needs no new API key beyond ``OPENALGO_API_KEY`` (which the engine already
requires).

It turns *tradable* instruments into macro proxies:

    currency        <- USD/INR (CDS / currency segment) close & change
    commodity legs  <- Gold, Crude (MCX) closes & changes
    index level     <- NIFTY / BANKNIFTY closes (a market-confirmation input)

plus an optional G-Sec yield reading for ``bond_return`` when the broker
exposes the 10Y benchmark on NSE (``exchange='NSE'``, ``symbol='10YBENCHMARK'``
or similar - broker dependent).

Design rules
------------
* Uses ONLY ``client.history`` (and optionally ``client.quotes``). No internals.
* Every SDK response is checked for ``status == 'success'`` (SDK contract).
* A missing symbol / failed call is a *warning*, never an exception out of
  :meth:`_fetch` (which is itself wrapped by :meth:`MacroProvider.fetch`).
* Database-free and side-effect-free: read-only market data.

Note on the *purity* boundary
-----------------------------
``economic_engine.py`` stays pure; this provider is injected into it. The SDK
client is passed in by the caller (pipeline / runner), keeping construction
testable with a fake client.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from .base import MacroProvider, value_block


def _last_close(client: Any, symbol: str, exchange: str,
                lookback_days: int = 10) -> Optional[Dict[str, Any]]:
    """Return the two most recent daily closes for symbol/exchange, or None.

    Uses ``client.history(symbol=..., exchange=..., interval='D', start_date,
    end_date)`` per the OpenAlgo History API. Trims to the last two valid rows
    so the change / previous can be computed without assuming calendar length.
    """
    end = datetime.now()
    start = end - timedelta(days=lookback_days)
    try:
        resp = client.history(
            symbol=symbol, exchange=exchange, interval="D",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
        )
    except Exception:  # noqa: BLE001 - SDK call boundary
        return None
    if not isinstance(resp, dict) or resp.get("status") != "success":
        return None
    rows = resp.get("data") or []
    if not isinstance(rows, list) or not rows:
        return None
    valid = [r for r in rows if isinstance(r, dict)
             and r.get("close") is not None]
    if not valid:
        return None
    last = valid[-1]
    prev = valid[-2] if len(valid) >= 2 else None
    return {
        "close": last.get("close"),
        "previous": (prev or {}).get("close"),
        "timestamp": last.get("timestamp"),
    }


class OpenAlgoMacroProvider(MacroProvider):
    """Derive macro proxies from the OpenAlgo SDK (no extra credentials)."""

    name = "openalgo"

    def __init__(self, client: Any,
                 config: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None):
        super().__init__(config, log)
        self.client = client

        # Default instrument map (config overrides). Each entry is
        # (signal_key, symbol, exchange, unit). Brokers differ on symbols for
        # FX / benchmark yields, hence the config escape hatch.
        self.instruments: List[Dict[str, str]] = list(self.config.get(
            "instruments") or [
                {"signal": "currency", "symbol": "USDINR", "exchange": "CDS",
                 "unit": "usd_inr"},
                {"signal": "gold", "symbol": "GOLD", "exchange": "MCX",
                 "unit": "price"},
                {"signal": "crude", "symbol": "CRUDEOIL", "exchange": "MCX",
                 "unit": "price"},
                {"signal": "nifty", "symbol": "NIFTY", "exchange": "NSE_INDEX",
                 "unit": "index"},
                {"signal": "banknifty", "symbol": "BANKNIFTY",
                 "exchange": "NSE_INDEX", "unit": "index"},
            ])

    def _fetch(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        warnings: List[str] = []

        if self.client is None:
            warnings.append("no SDK client supplied to OpenAlgoMacroProvider")
            out["meta"] = {"warnings": warnings, "ok": False}
            return out

        for spec in self.instruments:
            key = spec.get("signal")
            sym = spec.get("symbol")
            exch = spec.get("exchange", "NSE")
            if not key or not sym:
                continue
            row = _last_close(self.client, sym, exch)
            if row is None or row.get("close") is None:
                warnings.append(f"no history for {sym}/{exch} (signal '{key}')")
                continue

            close = row["close"]
            prev = row.get("previous")
            # Canonical macro block: currency feeds the 'currency' signal
            # directly; commodities/indices are surfaced as raw macro keys the
            # engine's Market stage and the FII-free breadth proxy can consume.
            out[key] = value_block(close, prev,
                                   unit=spec.get("unit"),
                                   source=f"openalgo:{exch}:{sym}")
            # A generic alias so the economic engine can read 'currency'.
            if key == "currency":
                out["currency"] = value_block(close, prev, unit="usd_inr",
                                              source=f"openalgo:{exch}:{sym}")

        out["meta"] = {"ok": not warnings or len(out) > 1,
                       "warnings": warnings, "source": self.name}
        return out


__all__ = ["OpenAlgoMacroProvider", "_last_close"]