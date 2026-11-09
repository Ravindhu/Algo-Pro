"""Indian equity cost model: local arithmetic (no Blue_engine import).

Flow stage: Indian cost model
Status: PHASE 1 IMPLEMENTED (stage 2 port 2026-11-06: arithmetic is local)
Ported from: Blue_engine/backtest/costs.py (CostModel.charges_bps, total_bps) and
Blue_engine/portfolio/optim/cost_aware.py (DEFAULT_COST_BPS: STT, GST, stamp, SEBI).
Parity against Blue: tests/parity/test_costs_parity.py (runs only where Blue loads).

RULES (owner-confirmed 2026-11-03)
----------------------------------
- Delivery: brokerage is zero. STT applies on BOTH buy and sell legs.
- Intraday: brokerage is 0.03% of notional capped at Rs 20 per order. STT applies on the
  sell leg only.
- Stamp duty applies on the buy leg only (delivery and intraday rates differ).
- GST (18%) applies to brokerage + exchange transaction charge + SEBI fee.
- ``impact_bps`` is a flat per-side cost (5 bps). Blue's square-root impact is not used
  (see docs/audits/PHASE0_REUSE_AUDIT.md, X-30).

PER-SIDE BPS (one fill)
-----------------------
    brokerage + stt + exchange_txn + sebi + gst + stamp_duty(buy only) + impact

RATE SOURCE
-----------
- STT, stamp duty, SEBI turnover fee, GST: NSE "First time investor" pages, updated 17/04/2026
  (checked 2026-11-05).
- Exchange transaction charge 0.00307% per side for NSE cash (delivery and intraday): NSE
  circular FA73061 (27 Feb 2026, effective 1 Mar 2026): Rs 307 per crore, incl. Rs 0.01 IPFT.
- Brokerage 0.03%, Rs 20 cap: owner's rule, not an exchange rate.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    brokerage_pct: float = 0.0003          # intraday only
    brokerage_cap: float = 20.0            # Rs per order, intraday only
    delivery_brokerage_pct: float = 0.0    # delivery brokerage is zero
    stt_delivery: float = 0.001            # delivery, both legs
    stt_intraday_sell: float = 0.00025     # intraday, sell leg only
    exchange_txn_pct: float = 0.0000307    # NSE circular FA73061, eff. 1 Mar 2026
    sebi_fee_pct: float = 0.000001
    stamp_buy_delivery: float = 0.00015
    stamp_buy_intraday: float = 0.00003
    gst_pct: float = 0.18
    impact_bps: float = 5.0

    def trade_cost(self, notional: float, side: str, product: str = "delivery") -> float:
        """Total statutory cost plus flat impact for one fill, in currency units."""
        return notional * self.total_bps(notional, side, product) / 10_000.0

    def total_bps(self, notional: float, side: str, product: str = "delivery") -> float:
        """All cost terms for one fill, in bps of notional."""
        # Refuse unknown strings: a typo like "sell" must not be charged as a buy.
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")
        if product not in ("delivery", "intraday"):
            raise ValueError(f"product must be 'delivery' or 'intraday', got {product!r}")
        if notional < 0:
            raise ValueError("notional must be >= 0")
        is_buy = side == "BUY"
        intraday = product == "intraday"

        if intraday:
            brokerage = self.brokerage_pct * 1e4
            if notional > 0:
                brokerage = min(brokerage, self.brokerage_cap / notional * 1e4)
            stt = 0.0 if is_buy else self.stt_intraday_sell * 1e4
            stamp = self.stamp_buy_intraday * 1e4 if is_buy else 0.0
        else:
            brokerage = self.delivery_brokerage_pct * 1e4
            stt = self.stt_delivery * 1e4          # both legs
            stamp = self.stamp_buy_delivery * 1e4 if is_buy else 0.0

        txn = self.exchange_txn_pct * 1e4
        sebi = self.sebi_fee_pct * 1e4
        gst = (brokerage + txn + sebi) * self.gst_pct   # GST on brokerage + txn + SEBI
        return brokerage + stt + txn + sebi + gst + stamp + self.impact_bps
