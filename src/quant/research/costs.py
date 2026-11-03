"""Indian equity cost model: thin wrapper over the Blue_engine cost model.

Flow stage: Indian cost model
Status: PHASE 0 IMPLEMENTED (wrapper: arithmetic delegated to Blue_engine; behaviour unchanged)
Existing code to reuse or wrap (do not copy): Blue_engine/backtest/costs.py

WHAT THIS FILE KEEPS
--------------------
The public parameters and ``trade_cost`` signature used by Phase 1 code and tests.
The statutory arithmetic (brokerage, exchange, SEBI, GST, STT, stamp, impact) is NOT
here. It is computed by ``blue_engine.backtest.costs.CostModel``.

WHAT THIS FILE TRANSLATES (only this)
-------------------------------------
- Currency brokerage cap -> bps for one trade, so Blue's percentage brokerage matches.
- Per-product rates (delivery vs intraday) -> one Blue config per leg.
- STT and stamp duty apply only on the leg they are charged on (kept as before this wrap).
- ``impact_bps`` (flat per-side bps) -> Blue's slippage term, with latency and sqrt impact off.

BEHAVIOUR IS UNCHANGED FROM BEFORE THE WRAP. Known questions, NOT changed here:
- STT on the BUY leg of delivery trades. The pre-wrap code charged STT on the SELL leg only.
  Verify against the current NSE/SEBI schedule before changing it (separate commit).
- Default rates are placeholders. Verify against current NSE, BSE and SEBI schedules before use.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict


@lru_cache(maxsize=1)
def _blue_cost_class() -> Any:
    """Resolve Blue_engine's CostModel once. Raises BlueEngineUnavailable if it cannot load."""
    from quant.core.blue_bridge import load
    return load("backtest.costs").CostModel


@dataclass(frozen=True)
class CostModel:
    brokerage_pct: float = 0.0003
    brokerage_cap: float = 20.0
    stt_delivery_sell: float = 0.001
    stt_intraday_sell: float = 0.00025
    exchange_txn_pct: float = 0.0000322
    sebi_fee_pct: float = 0.000001
    stamp_buy_delivery: float = 0.00015
    stamp_buy_intraday: float = 0.00003
    gst_pct: float = 0.18
    impact_bps: float = 5.0

    def trade_cost(self, notional: float, side: str, product: str = "delivery") -> float:
        """Total statutory cost plus flat impact for one fill, in currency units."""
        if notional < 0:
            raise ValueError("notional must be >= 0")
        is_buy = side != "SELL"
        intraday = product == "intraday"
        return notional * self._blue(notional, is_buy, intraday).total_bps(is_buy=is_buy) / 10_000.0

    def _blue(self, notional: float, is_buy: bool, intraday: bool) -> Any:
        """A Blue CostModel configured for one leg of one trade."""
        return _blue_cost_class()(
            cost_config=self._leg_config(notional, is_buy, intraday),
            slippage_bps=self.impact_bps,
            latency_bps=0.0,
            impact_coefficient=0.0,
        )

    def _leg_config(self, notional: float, is_buy: bool, intraday: bool) -> Dict[str, float]:
        """Map this model's currency and fraction fields onto Blue's bps keys for one leg."""
        brokerage_bps = self.brokerage_pct * 1e4
        if notional > 0:
            brokerage_bps = min(brokerage_bps, self.brokerage_cap / notional * 1e4)
        if is_buy:
            stt_bps = 0.0
            stamp_bps = (self.stamp_buy_intraday if intraday else self.stamp_buy_delivery) * 1e4
        else:
            stt_bps = (self.stt_intraday_sell if intraday else self.stt_delivery_sell) * 1e4
            stamp_bps = 0.0
        return {
            "brokerage": brokerage_bps,
            "stt": stt_bps,
            "exchange_txn": self.exchange_txn_pct * 1e4,
            "sebi": self.sebi_fee_pct * 1e4,
            "gst_on_brokerage_pct": self.gst_pct * 100.0,
            "stamp_duty": stamp_bps,
        }
