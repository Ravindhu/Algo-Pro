"""OCL strategy adapter: makes the OCL signals usable by the 5-minute strategy bot.

Flow stage: Strategy (OCL keylevel, sell-failure top/bottom)
Status: PHASE 3 IMPLEMENTED (2026-11-11)

WHY AN ADAPTER
--------------
The bot calls ``strategy.signal(df)`` on a closed-bar DataFrame and expects
``{"side", "entry", "sl_dist", "tp_dist", "atr", "time"}``. The OCL classes are
polling strategies: they own a ``client`` with ``.history()`` and return
``{"action", "quantity", "stop_loss", "target", ...}`` from ``detect()``.

This adapter does NOT re-implement OCL logic. It:
  1. builds a tiny history client that serves the bot's own closed bars, so OCL
     never reaches the network from inside the bot;
  2. calls ``detect()`` once per cycle;
  3. converts the OCL intent into the bot's signal contract.

LONG-ONLY (owner, 2026-11-09)
-----------------------------
The book is BUY and ADD only. A SELL from OCL is returned as a SELL signal and the
bot refuses it at scan time (``long_only_refused``). The adapter never flips a side.

BOUNDARY
--------
No order client, no gate, no sizing. The bot's risk adapter does all of that.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

__all__ = ["BarsHistoryClient", "OclStrategyAdapter", "OCL_STRATEGIES"]


class BarsHistoryClient:
    """Minimal stand-in for the OpenAlgo client: serves the bot's closed bars.

    OCL calls ``client.history(symbol=..., exchange=..., interval=..., start_date=...,
    end_date=...)`` and reads ``response["data"]`` as a DataFrame. This client returns
    the bars it was given, whatever interval is asked for, and never touches the network.
    """

    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    def history(self, symbol: str = "", exchange: str = "", interval: str = "",
                start_date: str = "", end_date: str = "", **_: Any) -> Dict[str, Any]:
        if self._df is None or len(self._df) == 0:
            return {"status": "error", "data": None}
        return {"status": "success", "data": self._df.copy()}


class OclStrategyAdapter:
    """Wrap one OCL signal class so the bot can call ``signal(df)``.

    ``name`` is the registry key used in config/bot.yaml strategies.
    """

    def __init__(self, name: str, ocl_cls, config: Optional[Dict[str, Any]] = None) -> None:
        self.name = name
        self._cls = ocl_cls
        self._config = dict(config or {})
        self._impl = None  # built lazily: OCL needs a client, which needs the bars

    def _build(self, df: pd.DataFrame):
        cfg = dict(self._config)
        cfg.setdefault("symbol", cfg.get("symbol", "PORTFOLIO"))
        return self._cls(BarsHistoryClient(df), cfg)

    def signal(self, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        if df is None or len(df) < 5:
            return None
        if self._impl is None:
            self._impl = self._build(df)
        else:
            # Refresh the bars the OCL instance sees (same object, new data).
            self._impl.client = BarsHistoryClient(df)
        try:
            intent = self._impl.detect()
        except Exception:
            return None  # a strategy error must never stop the cycle; the bot logs it
        if not isinstance(intent, dict):
            return None
        action = str(intent.get("action", "")).upper()
        if action not in ("BUY", "SELL"):
            return None
        entry = intent.get("entry_price") or intent.get("ltp") or intent.get("price")
        stop = intent.get("stop_loss")
        target = intent.get("target")
        if not all(isinstance(v, (int, float)) and v > 0 for v in (entry, stop, target)):
            return None
        entry = float(entry)
        sl_dist = abs(entry - float(stop))
        tp_dist = abs(float(target) - entry)
        if sl_dist <= 0 or tp_dist <= 0:
            return None
        atr = intent.get("atr")
        if not isinstance(atr, (int, float)) or atr <= 0:
            atr = sl_dist  # OCL's stop distance is the risk unit when no ATR is given
        return {
            "side": action,
            "entry": entry,
            "sl_dist": sl_dist,
            "tp_dist": tp_dist,
            "atr": float(atr),
            "time": str(intent.get("time") or df.index[-2]),
            "strategy": self.name,
        }


def _ocl_classes() -> Dict[str, Any]:
    """Registry: name -> OCL class. Imported lazily so a missing optional dep cannot break the bot."""
    from quant.strategies.ocl.ocl import OclSignal
    from quant.strategies.ocl.ocl_sell_fail_bottom import OclSellFailBottomSignal
    from quant.strategies.ocl.ocl_sell_fail_top import OclSellFailTopSignal

    return {
        "ocl": OclSignal,
        "ocl_sell_fail_top": OclSellFailTopSignal,
        "ocl_sell_fail_bottom": OclSellFailBottomSignal,
    }


def OCL_STRATEGIES() -> Dict[str, Any]:
    """Names the bot runner can wire. Empty when the OCL package cannot import."""
    try:
        return _ocl_classes()
    except Exception:
        return {}
