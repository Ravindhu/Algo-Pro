"""Broker brokerage model for the trade setup: product (delivery/intraday) x
method (fixed rupees per order, or bps of notional).

Owner request 2026-11-18: each broker has a different method, so the dashboard
lets the owner pick the product and the method, and BOTH calculations exist in
code:

- ``fixed``  -> ``flat_brokerage_per_order`` (rupees per executed order). The
  same rupees on a Rs 2,000 order and a Rs 2,00,000 order: 100 bps and 1 bp.
- ``bps``    -> ``brokerage`` (bps of notional per side). Scales with the ticket.

The saved block maps straight onto ``backtest/costs.CostModel`` (decision 0037),
so the backtest, the planner and the paper bot all charge the broker the way the
owner's broker actually does.

Flow stage: Risk gate (cost inputs for sizing and backtest)
Status: PHASE 1 IMPLEMENTED
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

__all__ = ["PRODUCTS", "METHODS", "DEFAULT_SETUP", "STT_BPS",
           "validate_brokerage", "brokerage_charge", "stt_charge",
           "cost_config", "charge_summary"]

PRODUCTS = ("delivery", "intraday")
METHODS = ("fixed", "bps")

#: STT in bps of notional, per side, charged on BOTH sides in this model.
#: Intraday buy is actually STT-free in India; charging 2.5 bps on both sides
#: over-states cost by 2.5 bps on the round trip — the conservative error, and
#: it is stated in the CostModel docstring (decision 0037).
STT_BPS = {"delivery": 10.0, "intraday": 2.5}

#: The shape saved in settings.yaml under trade_setup.brokerage. Null = not set:
#: the charge falls back to the bps default and the dashboard shows "not set".
DEFAULT_SETUP: Dict[str, Any] = {
    "product": "delivery",
    "method": "bps",
    "fixed_per_order": None,   # rupees per executed order
    "bps": None,               # bps of notional per side
}


def validate_brokerage(raw: Any) -> Dict[str, Any]:
    """Clean one brokerage block. Raises ValueError on anything unusable."""
    if not isinstance(raw, Mapping):
        raise ValueError("brokerage must be an object with product, method and the rate")
    out = dict(DEFAULT_SETUP)
    product = raw.get("product", out["product"])
    if product not in PRODUCTS:
        raise ValueError(f"brokerage.product must be one of: {', '.join(PRODUCTS)}")
    out["product"] = product
    method = raw.get("method", out["method"])
    if method not in METHODS:
        raise ValueError(f"brokerage.method must be one of: {', '.join(METHODS)}")
    out["method"] = method

    fixed = raw.get("fixed_per_order", out["fixed_per_order"])
    if fixed is not None:
        if isinstance(fixed, bool) or not isinstance(fixed, (int, float)) \
                or not math.isfinite(fixed) or fixed <= 0:
            raise ValueError("brokerage.fixed_per_order must be a positive number of rupees")
        out["fixed_per_order"] = float(fixed)
    bps = raw.get("bps", out["bps"])
    if bps is not None:
        if isinstance(bps, bool) or not isinstance(bps, (int, float)) \
                or not math.isfinite(bps) or bps < 0:
            raise ValueError("brokerage.bps must be zero or more (bps of notional)")
        out["bps"] = float(bps)
    if out["method"] == "fixed" and out["fixed_per_order"] is None:
        raise ValueError("brokerage.method 'fixed' needs brokerage.fixed_per_order")
    if out["method"] == "bps" and out["bps"] is None:
        raise ValueError("brokerage.method 'bps' needs brokerage.bps")
    return out


def brokerage_charge(notional: float, setup: Optional[Mapping[str, Any]] = None) -> float:
    """Brokerage in RUPEES for one order, by the configured method.

    ``fixed``: the rupee fee per order, whatever the ticket is.
    ``bps``:   notional * bps / 10000.
    Unset rate falls back to the bps default of 0 (the owner must set it).
    """
    s = setup or DEFAULT_SETUP
    method = s.get("method")
    if method == "fixed":
        fixed = s.get("fixed_per_order")
        return float(fixed) if fixed is not None else 0.0
    bps = s.get("bps")
    if bps is None:
        return 0.0
    return float(notional) * float(bps) / 10000.0


def stt_charge(notional: float, product: Optional[str] = None) -> float:
    """STT in rupees for one side, from the product."""
    bps = STT_BPS[product if product in PRODUCTS else "delivery"]
    return float(notional) * bps / 10000.0


def cost_config(setup: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The cost block ``backtest/costs.CostModel`` reads (decision 0037).

    A flat fee replaces the bps brokerage rate (a broker charges one or the
    other), and STT follows the product. Everything else stays at the
    cost_aware defaults.
    """
    s = setup or DEFAULT_SETUP
    product = s.get("product") if s.get("product") in PRODUCTS else "delivery"
    cfg: Dict[str, Any] = {"stt": STT_BPS[product]}
    if s.get("method") == "fixed" and s.get("fixed_per_order") is not None:
        cfg["flat_brokerage_per_order"] = float(s["fixed_per_order"])
        cfg["brokerage"] = 0.0
    else:
        cfg["brokerage"] = float(s.get("bps") or 0.0)
    return cfg


def charge_summary(setup: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """One screen for the dashboard: what a Rs 10,000 order costs under the setup."""
    s = setup or DEFAULT_SETUP
    notional = 10_000.0
    return {
        "product": s.get("product") if s.get("product") in PRODUCTS else "delivery",
        "method": s.get("method") if s.get("method") in METHODS else "bps",
        "example_notional": notional,
        "brokerage": round(brokerage_charge(notional, s), 2),
        "stt": round(stt_charge(notional, s.get("product")), 2),
        "round_trip": round(2 * (brokerage_charge(notional, s)
                                 + stt_charge(notional, s.get("product"))), 2),
    }
