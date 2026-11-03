"""Indian equity cost model: brokerage, STT, exchange charges, GST, stamp duty, impact.

Flow stage: Indian cost model
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/backtest/costs.py
NOTE: default rates are placeholders. Verify against current NSE, BSE and SEBI schedules before use.
"""
from __future__ import annotations

from dataclasses import dataclass


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
        if notional < 0:
            raise ValueError("notional must be >= 0")
        intraday = product == "intraday"
        brokerage = min(self.brokerage_pct * notional, self.brokerage_cap)
        exchange = self.exchange_txn_pct * notional
        sebi = self.sebi_fee_pct * notional
        gst = self.gst_pct * (brokerage + exchange + sebi)
        if side == "SELL":
            stt = (self.stt_intraday_sell if intraday else self.stt_delivery_sell) * notional
            stamp = 0.0
        else:
            stt = 0.0
            stamp = (self.stamp_buy_intraday if intraday else self.stamp_buy_delivery) * notional
        impact = self.impact_bps / 10_000 * notional
        return brokerage + exchange + sebi + gst + stt + stamp + impact
