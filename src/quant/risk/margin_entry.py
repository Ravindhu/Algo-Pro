"""Margin-funded delivery (MTF): which product a new delivery entry uses, and the leverage check. Pure, fail closed, OFF by default.

Flow stage: Risk gate (margin funding), entry product choice and leverage verification
Status: PHASE 2 IMPLEMENTED (decision logic; wired into the bot in strategy_bot._entry_product)
Rules (owner 2026-11-25):
- Only BUY delivery entries may become MTF, and only while the borrowed total stays within the limit.
- The broker's returned margin must be checked against the full value of the order before the order is sent
  (verify_leverage). The implied leverage must be above 1 and at most max_leverage.
- Any failed check keeps the entry as CNC. The margin path never blocks a CNC entry.
- Borrowed amount is counted at full notional (conservative).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from quant.risk.margin_funding import margin_exposure_check

MTF = "MTF"
CNC = "CNC"
MAX_BORROW_PCT = 1.0      # a share of capital above 100% is refused
MAX_LEVERAGE_CAP = 5.0    # 5x = 80% funding, the broker's stated maximum


@dataclass(frozen=True)
class MarginSettings:
    enabled: bool = False
    limit_inr: Optional[float] = None
    max_leverage: Optional[float] = None


def _num(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def settings_from_dict(settings: Mapping[str, Any]) -> MarginSettings:
    """Only the boolean true enables margin. The limit is mtf_borrow_pct x capital. max_leverage must be set (1, 5]."""
    margin = settings.get("margin") or {}
    enabled = margin.get("mtf_enabled") is True
    pct = _num(margin.get("mtf_borrow_pct"))
    capital = _num((settings.get("risk_budget") or {}).get("capital_inr"))
    max_lev = _num(margin.get("mtf_max_leverage"))
    limit = None
    if pct is not None and capital is not None and 0 < pct <= MAX_BORROW_PCT and capital > 0:
        limit = pct * capital
    if max_lev is not None and not (1.0 < max_lev <= MAX_LEVERAGE_CAP):
        max_lev = None
    return MarginSettings(enabled=enabled, limit_inr=limit, max_leverage=max_lev)


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
    """(product, reason). MTF is only a candidate here; the broker leverage check decides the final product."""
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


def verify_leverage(reply: Any, full_value: float, max_leverage: Optional[float]) -> Tuple[bool, str]:
    """(ok, reason). Compares the broker's returned margin with the full value of the order.

    reply is the OpenAlgo margin answer: {"status": "success", "data": {"total_margin_required": <rupees>}}.
    Refused unless the broker reports a usable margin that is below the full value and implies a leverage
    of at most max_leverage. A margin equal to or above the full value means no funding was applied.
    """
    if max_leverage is None:
        return False, "no maximum leverage set: refused"
    full = _num(full_value)
    if full is None or full <= 0:
        return False, "order value is not usable: refused"
    if not isinstance(reply, dict) or str(reply.get("status", "")).lower() != "success":
        return False, "broker margin check did not succeed: refused"
    data = reply.get("data")
    margin = _num(data.get("total_margin_required")) if isinstance(data, dict) else None
    if margin is None or margin <= 0:
        return False, "broker returned no usable margin: refused"
    if margin >= full:
        return False, "broker margin equals the full value: no funding applied, refused"
    leverage = full / margin
    if leverage > max_leverage:
        return False, f"implied leverage {leverage:.2f}x is above the maximum {max_leverage:.2f}x: refused"
    return True, f"broker margin {margin:.2f} of {full:.2f} (leverage {leverage:.2f}x)"
