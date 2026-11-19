"""Trade setup from the dashboard panel: risk per trade, max symbols, strategy, live cap and live IP.

Owner request 2026-11-07, live fields added 2026-11-15. A person sets these through the dashboard
API (owner sign-in required).
- sizing.risk_per_trade in config/settings.yaml: read by RiskLimits.risk_per_trade(). The bot reads it
  at start, so a change takes effect on the next start.
- max_symbols_per_portfolio: a risk limit in config/risk_limits.yaml. Saved through limits_store,
  and enforced by the Risk gate (risk/symbol_cap.py). Null (not set) blocks new symbols.
- live.max_capital_inr and live.allowed_egress_ip in config/settings.yaml: read by the live start gate.
- trade_setup.strategy in config/settings.yaml: saved only. No engine stage uses it yet.
- trade_setup.timeframes (htf, mtf, ltf): common to all strategies. Saved only, no engine
  stage reads them yet. Allowed values in TIMEFRAMES; when all three are set, LTF < MTF < HTF.
  Plan: docs/IMPLEMENTATION_PLAN_2026-11-18_TIMEFRAMES.md.
- trade_setup.brokerage: product (delivery/intraday) x method (fixed rupees per
  order, or bps of notional). Read by core/broker_costs.py, which maps it onto
  the backtest CostModel block (decision 0037) so every charging path uses the
  method the owner's broker actually charges.
Writes are atomic. Other keys are kept. Comments in settings.yaml are not kept on save.

Flow stage: Risk gate
Status: PHASE 3 IMPLEMENTED
"""
from __future__ import annotations

import ipaddress
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping

import yaml

from quant.core.config import RiskLimits
from quant.core.limits_store import apply_limit_updates
from quant.core import broker_costs

HEADER = (
    "# Global settings. sizing, trade_setup and live are set through the dashboard API (owner sign-in).\n"
    "# Other keys are edited by hand.\n"
)
DEFAULT_RISK = 0.0025
RISK_MAX = 0.05          # same upper bound as RiskLimits.risk_per_trade
SYMBOLS_MIN, SYMBOLS_MAX = 1, 100
KEYS = ("risk_per_trade", "max_symbols_per_portfolio", "strategy",
        "live_max_capital_inr", "live_allowed_egress_ip", "brokerage",
        "htf_interval", "mtf_interval", "ltf_interval")
LIVE_KEYS = ("live_max_capital_inr", "live_allowed_egress_ip")
# Timeframe labels, same style as the OCL params (htf_interval, ltf_interval in config/bot.yaml).
TIMEFRAMES = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}
TF_KEYS = {"htf_interval": "htf", "mtf_interval": "mtf", "ltf_interval": "ltf"}  # API key -> yaml key


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


def read_setup(settings_path, strategies_dir, limits_path) -> Dict[str, Any]:
    """Current setup for the page. Unset values come back as null (risk falls back to the default)."""
    data = _load(Path(settings_path))
    risk = (data.get("sizing") or {}).get("risk_per_trade", DEFAULT_RISK)
    symbols = None
    if Path(limits_path).exists():
        symbols = RiskLimits.from_yaml(limits_path).max_symbols_per_portfolio
    live = data.get("live") or {}
    trade = data.get("trade_setup") or {}
    brokerage = trade.get("brokerage")
    summary = None
    if isinstance(brokerage, dict):
        summary = broker_costs.charge_summary(brokerage)
    tf = trade.get("timeframes") or {}
    return {
        "risk_per_trade": risk,
        "max_symbols_per_portfolio": symbols,
        "strategy": trade.get("strategy"),
        "strategies": strategy_names(strategies_dir),
        "live_max_capital_inr": live.get("max_capital_inr"),
        "live_allowed_egress_ip": live.get("allowed_egress_ip"),
        "brokerage": brokerage if isinstance(brokerage, dict) else None,
        "brokerage_example": summary,
        "htf_interval": tf.get("htf"),
        "mtf_interval": tf.get("mtf"),
        "ltf_interval": tf.get("ltf"),
        "timeframe_options": list(TIMEFRAMES),
    }


def load_timeframes(settings_path) -> Dict[str, Any]:
    """Saved trade setup timeframes as {"mtf": .., "htf": .., "ltf": ..}. Missing values are None."""
    tf = (_load(Path(settings_path)).get("trade_setup") or {}).get("timeframes") or {}
    return {k: tf.get(k) for k in ("mtf", "htf", "ltf")}


def check_timeframe_order(tf: Mapping[str, Any]) -> None:
    """LTF < HTF < MTF in minutes (MTF = multi timeframe, the largest). Checked only when all three are set."""
    if not all(tf.get(k) for k in ("ltf", "mtf", "htf")):
        return
    ltf, htf, mtf = (TIMEFRAMES[tf[k]] for k in ("ltf", "htf", "mtf"))
    if not ltf < htf < mtf:
        raise ValueError(
            f"timeframes must go LTF < HTF < MTF, got LTF {tf['ltf']}, HTF {tf['htf']}, MTF {tf['mtf']}")


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
        elif key == "max_symbols_per_portfolio":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or float(value) != int(value) or not SYMBOLS_MIN <= int(value) <= SYMBOLS_MAX:
                raise ValueError(f"max_symbols_per_portfolio must be a whole number {SYMBOLS_MIN}-{SYMBOLS_MAX}")
            clean[key] = int(value)
        elif key == "live_max_capital_inr":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or value <= 0:
                raise ValueError("live_max_capital_inr must be a positive number of rupees")
            clean[key] = float(value)
        elif key == "live_allowed_egress_ip":
            try:
                clean[key] = str(ipaddress.IPv4Address(str(value).strip()))
            except (ValueError, TypeError):
                raise ValueError("live_allowed_egress_ip must be an IPv4 address, e.g. 203.0.113.10")
        elif key == "brokerage":
            clean[key] = broker_costs.validate_brokerage(value)
        elif key in TF_KEYS:  # null clears the timeframe
            if value is not None and (not isinstance(value, str) or value not in TIMEFRAMES):
                raise ValueError(f"{key} must be one of: {', '.join(TIMEFRAMES)}")
            clean[key] = value
        else:  # strategy
            if value not in strategies:
                raise ValueError(f"unknown strategy: {value!r}. Choose one of: {', '.join(strategies) or 'none'}")
            clean[key] = value
    return clean


def apply_setup_updates(settings_path, strategies_dir, limits_path,
                        updates: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Save validated updates. Returns {"old": {...}, "new": {...}} for the changed keys."""
    settings = Path(settings_path)
    clean = validate_setup_updates(updates, strategy_names(strategies_dir))
    old: Dict[str, Any] = {}
    if "max_symbols_per_portfolio" in clean:
        limit_result = apply_limit_updates(limits_path, {"max_symbols_per_portfolio": clean["max_symbols_per_portfolio"]})
        old["max_symbols_per_portfolio"] = limit_result["old"]["max_symbols_per_portfolio"]
    file_keys = {"risk_per_trade", "strategy", "brokerage", *LIVE_KEYS, *TF_KEYS}
    if file_keys & set(clean):
        current = _load(settings)
        sizing = dict(current.get("sizing") or {})
        trade = dict(current.get("trade_setup") or {})
        live = dict(current.get("live") or {})
        if "risk_per_trade" in clean:
            old["risk_per_trade"] = sizing.get("risk_per_trade")
            sizing["risk_per_trade"] = clean["risk_per_trade"]
        if "strategy" in clean:
            old["strategy"] = trade.get("strategy")
            trade["strategy"] = clean["strategy"]
        if "brokerage" in clean:
            old["brokerage"] = trade.get("brokerage")
            trade["brokerage"] = clean["brokerage"]
        if set(TF_KEYS) & set(clean):
            tf = dict(trade.get("timeframes") or {})
            for api_key, yaml_key in TF_KEYS.items():
                if api_key in clean:
                    old[api_key] = tf.get(yaml_key)
                    tf[yaml_key] = clean[api_key]
            check_timeframe_order(tf)  # refuse before anything is written
            trade["timeframes"] = tf
        if "live_max_capital_inr" in clean:
            old["live_max_capital_inr"] = live.get("max_capital_inr")
            live["max_capital_inr"] = clean["live_max_capital_inr"]
        if "live_allowed_egress_ip" in clean:
            old["live_allowed_egress_ip"] = live.get("allowed_egress_ip")
            live["allowed_egress_ip"] = clean["live_allowed_egress_ip"]
        current["sizing"] = sizing
        current["trade_setup"] = trade
        current["live"] = live
        body = HEADER + yaml.safe_dump(current, sort_keys=False)
        tmp = settings.with_name(settings.name + ".tmp")
        settings.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, settings)
    return {"old": old, "new": clean}
