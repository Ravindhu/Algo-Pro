"""Trade setup: risk per trade, trades per day, strategy. Saved to config/settings.yaml.

Owner request 2026-11-07. A person sets these through the dashboard API (token required).
- sizing.risk_per_trade: read by RiskLimits.risk_per_trade(), so the saved value is used by code.
- trade_setup.max_trades_per_day and trade_setup.strategy: saved only. The Risk gate does not
  enforce the trade cap yet, and no engine stage acts on the strategy choice yet.
Writes are atomic. Other keys are kept. Comments in settings.yaml are not kept on save.

Flow stage: Dashboard
Status: PHASE 3 IMPLEMENTED (config only)
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping

import yaml

HEADER = (
    "# Global settings. trade_setup and sizing are set through the dashboard API (token required).\n"
    "# Other keys are edited by hand.\n"
)
DEFAULT_RISK = 0.0025
RISK_MAX = 0.05          # same upper bound as RiskLimits.risk_per_trade
TRADES_MIN, TRADES_MAX = 1, 100
KEYS = ("risk_per_trade", "max_trades_per_day", "strategy")


def strategy_names(strategies_dir) -> List[str]:
    """Strategy names = file stems of config/strategies/*.yaml. Missing folder gives an empty list."""
    folder = Path(strategies_dir)
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.yaml"))


def _load(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def read_setup(settings_path, strategies_dir) -> Dict[str, Any]:
    """Current setup for the page. Unset values come back as null (risk falls back to the default)."""
    data = _load(Path(settings_path))
    risk = (data.get("sizing") or {}).get("risk_per_trade", DEFAULT_RISK)
    trade = data.get("trade_setup") or {}
    return {
        "risk_per_trade": risk,
        "max_trades_per_day": trade.get("max_trades_per_day"),
        "strategy": trade.get("strategy"),
        "strategies": strategy_names(strategies_dir),
    }


def validate_setup_updates(updates: Mapping[str, Any], strategies: List[str]) -> Dict[str, Any]:
    """Clean dict of updates. Raises ValueError on a bad key or value."""
    if not isinstance(updates, Mapping) or not updates:
        raise ValueError("body must be a non-empty JSON object")
    clean: Dict[str, Any] = {}
    for key, value in updates.items():
        if key not in KEYS:
            raise ValueError(f"unknown setup key: {key}")
        if key == "risk_per_trade":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or not 0 < value <= RISK_MAX:
                raise ValueError(f"risk_per_trade must be a fraction in (0, {RISK_MAX}]")
            clean[key] = float(value)
        elif key == "max_trades_per_day":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or float(value) != int(value) or not TRADES_MIN <= int(value) <= TRADES_MAX:
                raise ValueError(f"max_trades_per_day must be a whole number {TRADES_MIN}-{TRADES_MAX}")
            clean[key] = int(value)
        else:  # strategy
            if value not in strategies:
                raise ValueError(f"unknown strategy: {value!r}. Choose one of: {', '.join(strategies) or 'none'}")
            clean[key] = value
    return clean


def apply_setup_updates(settings_path, strategies_dir, updates: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Merge validated updates into settings.yaml. Returns {"old": {...}, "new": {...}}."""
    path = Path(settings_path)
    clean = validate_setup_updates(updates, strategy_names(strategies_dir))
    current = _load(path)
    sizing = dict(current.get("sizing") or {})
    trade = dict(current.get("trade_setup") or {})
    old: Dict[str, Any] = {}
    if "risk_per_trade" in clean:
        old["risk_per_trade"] = sizing.get("risk_per_trade")
        sizing["risk_per_trade"] = clean["risk_per_trade"]
    for key in ("max_trades_per_day", "strategy"):
        if key in clean:
            old[key] = trade.get(key)
            trade[key] = clean[key]
    current["sizing"] = sizing
    current["trade_setup"] = trade
    body = HEADER + yaml.safe_dump(current, sort_keys=False)
    tmp = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return {"old": old, "new": clean}
