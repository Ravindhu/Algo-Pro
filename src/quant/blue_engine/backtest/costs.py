"""
backtest/costs.py - the cost, slippage and latency model. First-class, not an
afterthought.

WHY THIS IS ITS OWN FILE
========================
The most common reason a backtest "works" and the live strategy loses money is
that the backtest never charged the trade. On Indian equity delivery the stack
is real and non-trivial: STT alone is 10 bps of notional on each side. A
strategy with a 12 bps per-trade edge is *underwater* once the round trip is
paid, and a simulator that ignores this will happily promote it.

This module owns the fill-cost interface and lazily reuses the published rates
from ``portfolio.optim.cost_aware``. The portfolio package is imported only when
constructing a model, not when importing the backtest package.

THE FOUR TERMS, AND WHY EACH IS SEPARATE
========================================
1. **STATUTORY / BROKERAGE** (brokerage, STT, exchange txn, GST, SEBI, stamp).
   These are facts, published by the exchange and the government. They are
   modelled from ``cost_aware.DEFAULT_COST_BPS`` and overridable per run. An
   error here is an error in every number the system produces, so the rates are
   named constants with their real-world source, not magic numbers inline.

2. **SLIPPAGE** - the gap between the price we expected and the price we got.
   This is NOT a statutory cost; it is a market cost that depends on liquidity,
   urgency and size. It is kept separate because it is the term Loop C can
   *calibrate from live fills*. A cost model that folds slippage into "charges"
   loses the ability to correct itself against reality.

3. **LATENCY / TIMING** - the gap between the bar we decided on and the price we
   can actually transact at. Modelled as a fixed penalty per position change
   because the honest simulator fills at the next bar's open, and the market
   moved between the decision and the fill. Zeroing this is how a backtest
   manufactures an edge that only exists at the exact close.

4. **MARKET IMPACT** - our own footprint, growing with size relative to volume.
   Only meaningful for sizeable orders; for the retail/small-institutional book
   this system runs, it is bounded and modelled as a square-root term below.
   Kept separate so that when the book size changes the model changes with it
   rather than silently under-charging.

The total charged on a fill is (1) + (2) + (3) + (4). Everything is in basis
points of traded notional, so a report can decompose a strategy's P&L into
gross edge and each cost term - which is the only way to answer "does this edge
survive costs, and which cost is killing it?".
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from . import config, logger

__all__ = ["CostModel", "CostBreakdown", "DEFAULT_MODEL"]


@dataclass(frozen=True)
class CostBreakdown:
    """One fill's cost, itemised. Frozen: a cost that can be mutated after the
    fact is a cost that can be retro-fitted to whatever answer we wanted."""

    notional: float
    #: Statutory + brokerage stack, in bps of notional, per SIDE.
    charges_bps: float
    #: Modelled market slippage, bps of notional, per SIDE.
    slippage_bps: float
    #: Timing/latency penalty, bps of notional, per SIDE.
    latency_bps: float
    #: Modelled own-impact, bps of notional, per SIDE.
    impact_bps: float
    #: True when this fill was a BUY (stamp duty is buy-side only).
    is_buy: bool

    @property
    def total_bps(self) -> float:
        return (self.charges_bps + self.slippage_bps
                + self.latency_bps + self.impact_bps)

    @property
    def total_cost(self) -> float:
        """Cost in currency units."""
        return self.notional * self.total_bps / 10000.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "notional": round(self.notional, 2),
            "charges_bps": round(self.charges_bps, 4),
            "slippage_bps": round(self.slippage_bps, 4),
            "latency_bps": round(self.latency_bps, 4),
            "impact_bps": round(self.impact_bps, 4),
            "total_bps": round(self.total_bps, 4),
            "total_cost": round(self.total_cost, 4),
            "is_buy": self.is_buy,
        }


class CostModel:
    """The full Indian cost stack for one instrument/run.

    Args:
        cost_config: overrides merged over ``cost_aware.DEFAULT_COST_BPS``. The
            keys are the same ones ``portfolio.optim.cost_aware`` uses, so a
            single config block configures the planner and the simulator.
        slippage_bps: modelled slippage on top of the statutory stack. This is
            the term Loop C calibrates from live fills.
        latency_bps: timing penalty per position change.
        impact_coefficient: the square-root market-impact coefficient. Impact is
            ``coef * sqrt(order_notional / bar_notional)`` in bps. 0 disables it.
        cost_aware: charge a round trip on *both* legs. True by default. Setting
            it False is only correct for a fill model that has already priced
            the exit, which this engine has not - so leave it True.
    """

    def __init__(self, *, cost_config: Optional[Dict[str, Any]] = None,
                 slippage_bps: float = config.DEFAULT_SLIPPAGE_BPS,
                 latency_bps: float = config.DEFAULT_LATENCY_BPS,
                 impact_coefficient: float = 8.0,
                 charge_both_legs: bool = True) -> None:
        from ..portfolio.optim.cost_aware import estimate_cost_bps
        self.rates = estimate_cost_bps(cost_config)
        for key, value in (cost_config or {}).items():
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(numeric):
                self.rates[key] = numeric
        self.slippage_bps = float(slippage_bps)
        self.latency_bps = float(latency_bps)
        self.impact_coefficient = float(impact_coefficient)
        self.charge_both_legs = bool(charge_both_legs)
        if (not all(math.isfinite(value) for value in
                    (self.slippage_bps, self.latency_bps,
                     self.impact_coefficient))
                or min(self.slippage_bps, self.latency_bps,
                       self.impact_coefficient) < 0):
            raise ValueError("cost model rates and impact coefficient must be finite and non-negative")

    # -- the statutory stack ------------------------------------------------ #

    def charges_bps(self, *, is_buy: bool) -> float:
        """Per-side statutory + brokerage cost in bps, incl. GST on brokerage+txn.

        Stamp duty is charged on the BUY leg only (it is a government levy on the
        purchase, not the sale), so this is the one term that is side-dependent.
        Getting this wrong by charging it on both sides over-states cost by
        ~1.5 bps per round trip, which matters at the edge sizes we trade.
        """
        r = self.rates
        brokerage = r.get("brokerage", 0.0)
        stt = r.get("stt", 0.0)
        txn = r.get("exchange_txn", 0.0)
        sebi = r.get("sebi", 0.0)
        gst = (brokerage + txn + sebi) * (r.get("gst_on_brokerage_pct", 0.0) / 100.0)
        total = brokerage + stt + txn + sebi + gst
        if is_buy:
            total += r.get("stamp_duty", 0.0)
        return total

    def round_trip_bps(self) -> float:
        """Full round-trip statutory cost in bps (buy leg + sell leg)."""
        return self.charges_bps(is_buy=True) + self.charges_bps(is_buy=False)

    # -- the market terms --------------------------------------------------- #

    def impact_bps(self, *, order_notional: float,
                   bar_notional: Optional[float]) -> float:
        """Modelled own-impact, bps. Square-root model, bounded.

        WHY SQUARE ROOT: the empirical market-impact literature (Almgren-Chriss
        and the square-root law that followed it) finds impact grows
        approximately with the square root of participation, not linearly. A
        linear model over-charges small orders and under-charges large ones -
        exactly the two cases a strategy's position sizing lives in.

        Returns 0 when either notional is unknown or non-positive: an unknown
        volume is not a reason to invent an impact figure.
        """
        if self.impact_coefficient <= 0:
            return 0.0
        if not bar_notional or bar_notional <= 0 or order_notional <= 0:
            return 0.0
        import math
        participation = order_notional / bar_notional
        return self.impact_coefficient * math.sqrt(participation)

    def total_bps(self, *, is_buy: bool, order_notional: float = 0.0,
                  bar_notional: Optional[float] = None,
                  extra_slippage_bps: float = 0.0) -> float:
        """All four terms for one fill, in bps."""
        return (self.charges_bps(is_buy=is_buy)
                + self.slippage_bps + self.latency_bps
                + self.impact_bps(order_notional=order_notional,
                                  bar_notional=bar_notional)
                + float(extra_slippage_bps))

    # -- the fill price ------------------------------------------------------ #

    def fill_price(self, *, side: str, reference_price: float,
                   order_notional: float = 0.0,
                   bar_notional: Optional[float] = None) -> Dict[str, Any]:
        """The price a market order actually transacts at, given the reference.

        The sign convention is the trader's, not the accountant's: slippage and
        impact always move the fill AGAINST us. A BUY fills *above* the
        reference; a SELL fills *below* it. Modelling favourable slippage is how
        a backtest earns money that the market never paid.
        """
        slip = (self.slippage_bps + self.latency_bps
                + self.impact_bps(order_notional=order_notional,
                                  bar_notional=bar_notional))
        adverse = 1.0 + slip / 10000.0
        if str(side).upper() == "SELL":
            adverse = 1.0 / adverse          # selling lower, symmetrically
        price = float(reference_price) * adverse
        return {"price": price, "slippage_bps": slip,
                "charges_bps": self.charges_bps(is_buy=str(side).upper() == "BUY")}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rates": dict(self.rates),
            "slippage_bps": self.slippage_bps,
            "latency_bps": self.latency_bps,
            "impact_coefficient": self.impact_coefficient,
            "charge_both_legs": self.charge_both_legs,
            "round_trip_bps": round(self.round_trip_bps(), 4),
        }

    # -- calibration (Loop C) ----------------------------------------------- #

    def calibrated(self, *, slippage_bps: Optional[float] = None,
                   reason: str = "") -> "CostModel":
        """A COPY with the slippage assumption replaced. The original is untouched.

        Loop C calls this with the slippage it measured from live fills. The
        engine that produced the old numbers must not be retro-actively changed
        - that would make a past backtest's report irreproducible. A new model
        for the next backtest is the honest move, and the reason string travels
        with it so a report can say *why* the assumption moved.
        """
        new = CostModel(cost_config=dict(self.rates),
                        slippage_bps=(self.slippage_bps if slippage_bps is None
                                      else float(slippage_bps)),
                        latency_bps=self.latency_bps,
                        impact_coefficient=self.impact_coefficient,
                        charge_both_legs=self.charge_both_legs)
        if reason:
            logger.info("cost model calibrated: %s (slippage %.2f -> %.2f bps)",
                        reason, self.slippage_bps, new.slippage_bps)
        return new


class _DefaultCostModelProxy:
    """Lazy compatibility proxy; keeps importing engine side-effect free."""

    def __init__(self) -> None:
        self._model: Optional[CostModel] = None
    def _get(self) -> CostModel:
        if self._model is None:
            self._model = CostModel()
        return self._model
    def __getattr__(self, name: str) -> Any:
        return getattr(self._get(), name)

    def __repr__(self) -> str:
        return "DEFAULT_MODEL (lazy CostModel)"

#: The default model: India delivery statutory stack + 5 bps slippage + 1 bps
#: latency. Constructed on first use, not during module import.
DEFAULT_MODEL = _DefaultCostModelProxy()
