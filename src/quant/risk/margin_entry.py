"""Margin-funded delivery (MTF) rules: limit, daily funded share from the broker, interest. Pure, fail closed, OFF by default.

Flow stage: Risk gate (margin funding)
Status: PHASE 3 IMPLEMENTED (rules; the bot stores one broker reading per stock per day)
Rules (owner 2026-11-25):
- Only BUY delivery entries may become MTF, and only while the borrowed total stays within the limit.
- The broker's margin for a 1-share order is read once per stock per day and stored (margin_daily.py).
  That reading gives the funded share. It must be above 0 and at most 1 - 1/max_leverage (5x = 80%).
- Interest: annual_rate (12%) on the borrowed amount, charged per day held (mtf_daily_interest).
- Any failed check keeps the entry as CNC. The margin path never blocks a CNC entry.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from quant.risk.margin_funding import margin_exposure_check

MTF = "MTF"
CNC = "CNC"
MAX_BORROW_PCT = 1.0       # a share of capital above 100% is refused
MAX_LEVERAGE_CAP = 5.0     # 5x = 80% funding, the broker's stated maximum
DEFAULT_ANNUAL_RATE = 0.12  # owner 2026-11-25: 12% a year on the borrowed amount
MAX_ANNUAL_RATE = 0.5


@dataclass(frozen=True)
class MarginSettings:
    enabled: bool = False
    limit_inr: Optional[float] = None
    max_leverage: Optional[float] = None
    annual_rate: Optional[float] = None


def _num(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def settings_from_dict(settings: Mapping[str, Any]) -> MarginSettings:
    """Only the boolean true enables margin. Limit = mtf_borrow_pct x capital. Unusable values mean no limit or no rate."""
    margin = settings.get("margin") or {}
    enabled = margin.get("mtf_enabled") is True
    pct = _num(margin.get("mtf_borrow_pct"))
    capital = _num((settings.get("risk_budget") or {}).get("capital_inr"))
    max_lev = _num(margin.get("mtf_max_leverage"))
    rate = DEFAULT_ANNUAL_RATE if "mtf_annual_rate" not in margin else _num(margin.get("mtf_annual_rate"))
    limit = None
    if pct is not None and capital is not None and 0 < pct <= MAX_BORROW_PCT and capital > 0:
        limit = pct * capital
    if max_lev is not None and not (1.0 < max_lev <= MAX_LEVERAGE_CAP):
        max_lev = None
    if rate is not None and not (0 < rate <= MAX_ANNUAL_RATE):
        rate = None
    return MarginSettings(enabled=enabled, limit_inr=limit, max_leverage=max_lev, annual_rate=rate)


def settings_from_path(settings_path) -> MarginSettings:
    """Read settings.yaml. Any read error gives a disabled MarginSettings (fail closed)."""
    import yaml
    try:
        data = yaml.safe_load(Path(settings_path).read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError("settings is not a mapping")
    except Exception:
        return MarginSettings()
    return settings_from_dict(data)


def choose_product(base: str, side: str, notional: float,
                   borrowed_now: float, cfg: Optional[MarginSettings]) -> Tuple[str, str]:
    """(product, reason). MTF is a candidate here; the day's broker reading decides the final product."""
    if base != CNC:
        return base, "not a delivery entry: margin not used"
    if cfg is None or not cfg.enabled:
        return CNC, "margin off"
    if str(side).upper() != "BUY":
        return CNC, "margin not used: only BUY entries"
    ok, reason = margin_exposure_check(True, cfg.limit_inr, borrowed_now, notional)
    if not ok:
        return CNC, f"margin not used: {reason}"
    return MTF, "within borrowed limit"


def funded_share(reply: Any, full_value: float, max_leverage: Optional[float]) -> Tuple[Optional[float], str]:
    """(funded share, reason). Funded share = 1 - margin / full value, from the broker's reply.

    reply is the OpenAlgo margin answer: {"status": "success", "data": {"total_margin_required": <rupees>}}.
    None means the reading is not usable: no maximum, a failed or malformed reply, a margin at or above the
    full value (no funding applied), or a funded share above 1 - 1/max_leverage.
    """
    if max_leverage is None:
        return None, "no maximum leverage set: refused"
    full = _num(full_value)
    if full is None or full <= 0:
        return None, "order value is not usable: refused"
    if not isinstance(reply, dict) or str(reply.get("status", "")).lower() != "success":
        return None, "broker margin check did not succeed: refused"
    data = reply.get("data")
    margin = _num(data.get("total_margin_required")) if isinstance(data, dict) else None
    if margin is None or margin <= 0:
        return None, "broker returned no usable margin: refused"
    if margin >= full:
        return None, "broker margin equals the full value: no funding applied, refused"
    funded = 1.0 - margin / full
    cap = 1.0 - 1.0 / max_leverage
    if funded > cap + 1e-12:
        return None, f"funded share {funded:.0%} is above the maximum {cap:.0%} ({max_leverage:.1f}x): refused"
    return funded, f"broker funds {funded:.0%} of the order"


def verify_leverage(reply: Any, full_value: float, max_leverage: Optional[float]) -> Tuple[bool, str]:
    """(ok, reason). Kept for the per-order check; funded_share holds the rule."""
    funded, reason = funded_share(reply, full_value, max_leverage)
    return (funded is not None), reason


def mtf_daily_interest(borrowed_inr: float, annual_rate: Optional[float]) -> float:
    """Interest for one day on a borrowed amount: borrowed x annual rate / 365. Zero or unusable inputs give 0."""
    borrowed = _num(borrowed_inr)
    rate = _num(annual_rate)
    if borrowed is None or rate is None or borrowed <= 0 or rate <= 0:
        return 0.0
    return borrowed * rate / 365.0
