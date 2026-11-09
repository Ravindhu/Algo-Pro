"""Validate and save user-set risk limits to config/risk_limits.yaml.

Owner decision 0020 (2026-11-06): limits are set by a person, through the dashboard API.
Owner decision 0031 (2026-11-09): money limits are in PERCENT of capital. Rupee keys are refused.
Null means "not set": the Risk gate then fails closed for entries.
Writes are atomic (temp file, then replace). The new file is loaded again before it replaces the old one.

Flow stage: Risk gate
Status: PHASE 1 IMPLEMENTED. Percent keys added 2026-11-09 (decision 0031).
Existing code to reuse or wrap (do not copy): none. Schema comes from core/config.py RiskLimits.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Dict, Mapping

import yaml

from quant.core.config import PERCENT_TO_RUPEE, RUPEE_KEYS, RiskLimits

HEADER = (
    "# Hard limits. Set by a person through the dashboard API (decisions 0020, 0031).\n"
    "# Money limits are PERCENT of capital_inr (0.005 = 0.5%). Null = not set: entries fail closed.\n"
    "# blocked: true stops all entries.\n"
)
PCT_KEYS = tuple(PERCENT_TO_RUPEE)
WRITABLE = ("blocked", "max_order_quantity", "max_adv_pct", "max_symbols_per_portfolio", "capital_inr") + PCT_KEYS


def validate_limit_updates(updates: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a clean dict of updates. Raises ValueError on any bad key or value."""
    if not isinstance(updates, Mapping) or not updates:
        raise ValueError("body must be a non-empty JSON object")
    clean: Dict[str, Any] = {}
    for key, value in updates.items():
        if key in RUPEE_KEYS:
            raise ValueError(f"{key} is retired: money limits are set in percent, use {_pct_name(key)}")
        if key not in WRITABLE:
            raise ValueError(f"unknown limit: {key}")
        if key == "blocked":
            if not isinstance(value, bool):
                raise ValueError("blocked must be true or false")
        elif value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} must be a number or null")
            if value < 0:
                raise ValueError(f"{key} must be zero or more")
            if key == "capital_inr" and value <= 0:
                raise ValueError("capital_inr must be more than zero")
            if (key == "max_adv_pct" or key in PCT_KEYS) and not 0 < value <= 1:
                raise ValueError(f"{key} must be a fraction between 0 and 1 (0.005 = 0.5%)")
            if key in ("max_order_quantity", "max_symbols_per_portfolio"):
                if float(value) != int(value):
                    raise ValueError(f"{key} must be a whole number")
                value = int(value)
        clean[key] = value
    return clean


def _pct_name(rupee_key: str) -> str:
    return next(p for p, r in PERCENT_TO_RUPEE.items() if r == rupee_key)


def apply_limit_updates(path, updates: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Merge validated updates into the limits file. Returns {"old": {...}, "new": {...}} for changed keys."""
    path = Path(path)
    clean = validate_limit_updates(updates)
    current = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    current = dict(current or {})
    old = {k: current.get(k) for k in clean}
    merged = {name: current.get(name) for name in WRITABLE}
    merged["blocked"] = bool(current.get("blocked", False))
    merged.update(clean)
    body = HEADER + yaml.safe_dump(merged, sort_keys=False)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    try:
        RiskLimits.from_yaml(tmp)  # final schema check before replacing the live file
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    return {"old": old, "new": clean}
