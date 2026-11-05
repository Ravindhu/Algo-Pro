"""Indian equity cost model: thin wrapper over the Blue_engine cost model.

Flow stage: Indian cost model
Status: PHASE 1 IMPLEMENTED (Phase 0 wrap: wrapper: arithmetic delegated to Blue_engine)
Existing code to reuse or wrap (do not copy): Blue_engine/backtest/costs.py

WHAT THIS FILE KEEPS
--------------------
The public parameters and ``trade_cost`` signature used by Phase 1 code and tests.
The statutory arithmetic (brokerage, exchange, SEBI, GST, STT, stamp, impact) is NOT
here. It is computed by ``blue_engine.backtest.costs.CostModel``.

RULES (owner-confirmed 2026-11-03)
----------------------------------
- Delivery: brokerage is zero. STT applies on BOTH buy and sell legs.
- Intraday: brokerage is 0.03% of notional capped at Rs 20 per order. STT applies on the
  sell leg only.
- Stamp duty applies on the buy leg only (delivery and intraday rates differ).
- ``impact_bps`` is a flat per-side cost, mapped to Blue's slippage term. Blue's
  square-root impact model is not used yet (see docs/audits/PHASE0_REUSE_AUDIT.md).

WHAT THIS FILE TRANSLATES (only this)
-------------------------------------
- Currency brokerage cap -> bps for one trade, so Blue's percentage brokerage matches.
- Per-product rules (delivery vs intraday) -> one Blue config per leg.

RATE SOURCE (checked 2026-11-05): NSE "First time investor" pages, updated 17/04/2026
(STT, turnover fees and stamp duty). Matches the defaults below: STT delivery 0.1% both
legs, STT intraday sell 0.025%, stamp delivery buy 0.015%, stamp intraday buy 0.003%,
SEBI turnover fee 0.0001%, GST 18%.
OPEN: exchange transaction charge (0.00322%) is NOT on those pages. Verify it on NSE's
transaction-charges page before live use. GST on exchange and SEBI charges is the standard
practice, not stated on those pages. Brokerage (0.03%, Rs 20 cap) is the owner's rule, not an exchange rate.
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
    brokerage_pct: float = 0.0003          # intraday only
    brokerage_cap: float = 20.0            # Rs per order, intraday only
    delivery_brokerage_pct: float = 0.0    # delivery brokerage is zero
    stt_delivery: float = 0.001            # delivery, both legs
    stt_intraday_sell: float = 0.00025     # intraday, sell leg only
    exchange_txn_pct: float = 0.0000322
    sebi_fee_pct: float = 0.000001
    stamp_buy_delivery: float = 0.00015
    stamp_buy_intraday: float = 0.00003
    gst_pct: float = 0.18
    impact_bps: float = 5.0

    def trade_cost(self, notional: float, side: str, product: str = "delivery") -> float:
        """Total statutory cost plus flat impact for one fill, in currency units."""
        # Refuse unknown strings: a typo like "sell" must not be charged as a buy.
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")
        if product not in ("delivery", "intraday"):
            raise ValueError(f"product must be 'delivery' or 'intraday', got {product!r}")
        if notional < 0:
            raise ValueError("notional must be >= 0")
        is_buy = side == "BUY"
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
        """Map this model's fields onto Blue's bps keys for one leg."""
        if intraday:
            brokerage_bps = self.brokerage_pct * 1e4
            if notional > 0:
                brokerage_bps = min(brokerage_bps, self.brokerage_cap / notional * 1e4)
            stt_bps = 0.0 if is_buy else self.stt_intraday_sell * 1e4
            stamp_bps = self.stamp_buy_intraday * 1e4 if is_buy else 0.0
        else:
            brokerage_bps = self.delivery_brokerage_pct * 1e4
            stt_bps = self.stt_delivery * 1e4          # both legs
            stamp_bps = self.stamp_buy_delivery * 1e4 if is_buy else 0.0
        return {
            "brokerage": brokerage_bps,
            "stt": stt_bps,
            "exchange_txn": self.exchange_txn_pct * 1e4,
            "sebi": self.sebi_fee_pct * 1e4,
            "gst_on_brokerage_pct": self.gst_pct * 100.0,
            "stamp_duty": stamp_bps,
        }
