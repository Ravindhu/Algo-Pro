"""Validate and save user-set risk limits to config/risk_limits.yaml.

Owner decision 0020 (2026-11-06): limits are set by a person, through the dashboard API.
Null means "not set": the Risk gate then fails closed for entries.
Writes are atomic (temp file, then replace). Inline YAML comments are not kept on save.

Flow stage: Risk gate
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): none. Schema comes from core/config.py RiskLimits.
"""
from __future__ import annotations

import math
import os
from dataclasses import fields
from pathlib import Path
from typing import Any, Dict, Mapping

import yaml

from quant.core.config import RiskLimits

HEADER = (
    "# Hard limits (INR). Set by a person through the dashboard API (decision 0020).\n"
    "# Null = not set: entries fail closed. blocked: true stops all entries.\n"
)
_NAMES = {f.name for f in fields(RiskLimits)}


def validate_limit_updates(updates: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a clean dict of updates. Raises ValueError on any bad key or value."""
    if not isinstance(updates, Mapping) or not updates:
        raise ValueError("body must be a non-empty JSON object")
    clean: Dict[str, Any] = {}
    for key, value in updates.items():
        if key not in _NAMES:
            raise ValueError(f"unknown limit: {key}")
        if key == "blocked":
            if not isinstance(value, bool):
                raise ValueError("blocked must be true or false")
        elif value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} must be a number or null")
            if value < 0:
                raise ValueError(f"{key} must be zero or more")
            if key == "max_adv_pct" and value > 1:
                raise ValueError("max_adv_pct must be between 0 and 1")
            if key in ("max_order_quantity", "max_symbols_per_portfolio"):
                if float(value) != int(value):
                    raise ValueError(f"{key} must be a whole number")
                value = int(value)
        clean[key] = value
    return clean


def apply_limit_updates(path, updates: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Merge validated updates into the limits file. Returns {"old": {...}, "new": {...}} for changed keys."""
    path = Path(path)
    clean = validate_limit_updates(updates)
    current = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    current = dict(current or {})
    old = {k: current.get(k) for k in clean}
    merged = {name: current.get(name) for name in _NAMES}
    merged["blocked"] = bool(current.get("blocked", False))
    merged.update(clean)
    RiskLimits(**merged)  # final schema check before writing
    body = HEADER + yaml.safe_dump(merged, sort_keys=False)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return {"old": old, "new": clean}
