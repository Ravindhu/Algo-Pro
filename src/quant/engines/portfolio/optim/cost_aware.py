"""
optim/cost_aware.py - India-realistic, cost-penalised rebalancing (pure).

A rebalance that ignores cost over-trades in backtest and bleeds live. This
module computes the *net-of-cost* desirability of moving from the current book to
a target, using Indian cost components, and exposes a **no-trade region** around
the optimum so small drifts are left alone.

Cost model (per side, in basis points of traded notional)
--------------------------------------------------------
* **brokerage**        per-order bps (config; often ~0-3 bps delivery).
* **STT**              Securities Transaction Tax: 0.1% on delivery buy & sell,
                       0.025% on intraday sell (India). Applied here as a bps rate.
* **exchange txn**     ~0.00325% NSE.
* **GST**              18% on (brokerage + txn + SEBI).
* **stamp duty**       0.015% on buy (delivery), 0.003% on sell.
* **SEBI**             ~0.0001%.
* **slippage**         expected market impact (bps) — the biggest term for size.
                       ONE setting (``DEFAULT_SLIPPAGE_BPS``) shared with the
                       backtest simulator; charged once per side, never in bps
                       AND as a price adjustment.
* **flat fee**         optional ``flat_brokerage_per_order`` (rupees per order),
                       used instead of ``brokerage`` bps when set. A flat fee
                       dominates small tickets, which a bps rate cannot show.

``turnover_bps``/``rebalance_cost`` sum these, so ONE config block drives the
simulator (``backtest/costs.py``) and this planner with no double-counting.

Defaults below are deliberately conservative (delivery-style); override from the
``costs`` config block. Turnover is one-way (``sum|t-c|/2``).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .base import gross_of
from .constraints import turnover
from ...core.results import safe_float

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_COST_BPS", "DEFAULT_SLIPPAGE_BPS", "estimate_cost_bps",
           "planner_costs", "rebalance_cost", "no_trade_region",
           "should_rebalance", "apply_band", "flat_brokerage_per_order"]

# Conservative India delivery defaults (bps of notional, per side).
DEFAULT_COST_BPS: Dict[str, float] = {
    "brokerage": 3.0,
    "stt": 10.0,           # 0.1% delivery
    "exchange_txn": 0.33,
    "gst_on_brokerage_pct": 18.0,
    "stamp_duty": 1.5,     # buy side, 0.015%
    "sebi": 0.01,
}

#: Expected market impact, bps per side. The ONE slippage setting in the system:
#: the backtest simulator reads it (``backtest/costs.py``) and the rebalancing
#: planner adds it per side, so a config change moves both. Kept out of
#: ``DEFAULT_COST_BPS`` so that dict holds only the statutory/brokerage stack
#: (rates that are certain), which the simulator can charge without the market
#: term being counted twice.
DEFAULT_SLIPPAGE_BPS = 5.0

#: Rates that are NOT per-notional: a flat fee, or a percent multiplier.
NON_BPS_KEYS = frozenset({"flat_brokerage_per_order", "gst_on_brokerage_pct"})


def _rate_bps(costs: Dict[str, float], key: str) -> float:
    """A rate that is bps of notional. A flat per-order fee is not one: a rupee
    amount scales with ticket size, so treating it as bps would over-charge a
    large order and under-charge a small one."""
    return 0.0 if key in NON_BPS_KEYS else float(costs.get(key, 0.0) or 0.0)


def flat_brokerage_per_order(costs: Dict[str, float]) -> float:
    """The flat rupee fee per executed order (0 when brokerage is a bps rate)."""
    try:
        return max(0.0, float(costs.get("flat_brokerage_per_order", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def planner_costs(config: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """Costs for the PLANNER: the statutory stack plus the shared slippage term.

    Why this exists. ``slippage`` left ``DEFAULT_COST_BPS`` because the backtest
    simulator must not receive it as a per-fill statutory rate. But the planner
    still has to charge expected market impact, or a rebalance would look free.
    This is the ONE place that adds it back, so a change to
    ``DEFAULT_SLIPPAGE_BPS`` (or to a config's ``slippage`` key) moves the
    planner, and the simulator picks it up from the same key.
    """
    costs = estimate_cost_bps(config)
    if config is None or safe_float((config or {}).get("slippage")) is None:
        costs.setdefault("slippage", DEFAULT_SLIPPAGE_BPS)
    return costs


def estimate_cost_bps(config: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """Resolve the per-side STATUTORY stack from config, merged over defaults.

    A ``slippage`` key in ``config`` is kept, so a caller that passes the full
    cost block gets it back unchanged. It is NOT in the defaults: the backtest
    simulator consumes this dict as per-fill statutory rates, and slippage there
    is a price move. Use :func:`planner_costs` to get the planner's view, which
    includes the default market term.
    """
    cfg = dict(DEFAULT_COST_BPS)
    for k, v in (config or {}).items():
        fv = safe_float(v)
        if fv is not None:
            cfg[k] = fv
    return cfg


def _round_trip_bps(costs: Dict[str, float]) -> float:
    """Total round-trip cost in bps (buy + sell), including GST on brokerage+txn.

    Slippage is charged ONCE per side and only when ``costs`` carries it. The
    caller opts in with :func:`planner_costs`, which is the single place that
    resolves the shared ``slippage`` key; ``CostModel`` (backtest/costs.py)
    charges the same key once in the fill price, so neither can double-charge
    it (audit item 1, 2026-11-17).
    """
    brokerage = _rate_bps(costs, "brokerage")
    txn = _rate_bps(costs, "exchange_txn")
    sebi = _rate_bps(costs, "sebi")
    gst_pct = float(costs.get("gst_on_brokerage_pct", 0.0) or 0.0) / 100.0
    gst = (brokerage + txn + sebi) * gst_pct
    slippage = float(costs.get("slippage", 0.0) or 0.0)
    per_side = (brokerage + _rate_bps(costs, "stt") + txn + sebi + gst + slippage)
    # Stamp duty is buy-side only.
    return 2.0 * per_side + _rate_bps(costs, "stamp_duty")


def rebalance_cost(target: Dict[str, float], current: Dict[str, float],
                   *, capital: Optional[float] = None,
                   config: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, Any]:
    """Estimate the cost of moving ``current -> target``.

    Returns ``{"turnover", "cost_bps", "cost_amount", "capital"}`` where
    ``turnover`` is one-way fraction and ``cost_amount`` is in currency units
    when ``capital`` is given.
    """
    costs = planner_costs(config)
    to = turnover(target, current)
    round_trip = _round_trip_bps(costs)
    # A one-way turnover of `to` incurs `to * round_trip` bps of notional.
    cost_bps = to * round_trip
    amount = None
    if capital:
        amount = round(to * float(capital) * round_trip / 10000.0, 2)
    return {"turnover": round(to, 6), "cost_bps": round(cost_bps, 4),
            "round_trip_bps": round(round_trip, 4), "cost_amount": amount,
            "capital": capital}


def no_trade_region(target: Dict[str, float], current: Dict[str, float],
                    *, band_pct: float = 2.0, cost_bps: Optional[float] = None
                    ) -> Dict[str, float]:
    """Compute the per-sleeve drift band below which trading is not worthwhile.

    The idea: a drift smaller than the round-trip cost (plus a safety multiple)
    cannot pay for itself, so it sits inside the "no-trade region". Returns a
    dict of per-sleeve absolute drift thresholds (fractions of weight).
    """
    costs = planner_costs()
    rt = (cost_bps if cost_bps is not None
          else _round_trip_bps(costs)) / 10000.0   # as a fraction
    # Band = max(configured %, round-trip cost as a fraction). The cost term is
    # the theoretical break-even; the % term is the operator's noise tolerance.
    threshold = max(band_pct / 100.0, rt)
    keys = set(target) | set(current)
    return {k: threshold for k in keys}


def should_rebalance(target: Dict[str, float], current: Dict[str, float], *,
                     band: Optional[Dict[str, float]] = None,
                     band_pct: float = 2.0) -> Dict[str, Any]:
    """Decide which sleeves breach the no-trade region.

    Returns ``{"rebalance": bool, "moves": {sleeve: delta}}`` with only the
    sleeves whose absolute drift exceeds their band.
    """
    bands = band or no_trade_region(target, current, band_pct=band_pct)
    moves: Dict[str, float] = {}
    for k in set(target) | set(current):
        t = safe_float(target.get(k)) or 0.0
        c = safe_float(current.get(k)) or 0.0
        if abs(t - c) >= bands.get(k, band_pct / 100.0):
            moves[k] = round(t - c, 6)
    return {"rebalance": bool(moves), "moves": moves}


def apply_band(target: Dict[str, float], current: Dict[str, float], *,
               band_pct: float = 2.0, costs_config: Optional[Dict[str, Any]]
               = None) -> Dict[str, Any]:
    """Return an *effective target* that leaves in-band sleeves unchanged.

    This is the practical rebalancer: sleeves inside their tolerance keep their
    current weight (so no order is generated for them); sleeves outside get the
    full target. Guarantees fewer, larger, cost-justified trades.
    """
    band_map = no_trade_region(
        target, current,
        band_pct=band_pct,
        cost_bps=_round_trip_bps(planner_costs(costs_config)))
    effective: Dict[str, float] = {}
    held: list = []
    for k in set(target) | set(current):
        t = safe_float(target.get(k)) or 0.0
        c = safe_float(current.get(k)) or 0.0
        if abs(t - c) < band_map.get(k, band_pct / 100.0):
            effective[k] = c
            held.append(k)
        else:
            effective[k] = t
    cost = rebalance_cost(effective, current, config=costs_config)
    return {"effective_target": {k: round(v, 8) for k, v in effective.items()},
            "held": sorted(held), "cost": cost}