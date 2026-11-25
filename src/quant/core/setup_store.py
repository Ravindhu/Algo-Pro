"""Trade setup from the dashboard panel: risk per trade, max symbols, strategy and live cap.

Owner request 2026-11-07, live fields added 2026-11-15. A person sets these through the dashboard
API (owner sign-in required).
- sizing.risk_per_trade in config/settings.yaml: read by RiskLimits.risk_per_trade(). The bot reads it
  at start, so a change takes effect on the next start.
- max_symbols_per_portfolio: a risk limit in config/risk_limits.yaml. Saved through limits_store,
  and enforced by the Risk gate (risk/symbol_cap.py). Null (not set) blocks new symbols.
- live.max_capital_inr in config/settings.yaml: read by the live start gate.
- risk_budget (capital_inr, max_daily_loss_pct, max_strategy_loss_pct) in config/settings.yaml: loss
  budgets as a share of capital (owner 2026-11-19). Saving them also writes the rupee caps
  max_daily_loss and max_strategy_loss in config/risk_limits.yaml.
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

import math
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping

import yaml

from quant.core.config import RiskLimits
from quant.core.limits_store import apply_limit_updates
from quant.core import broker_costs
from quant.engines.alpha.strategies.owner_patterns import SEQUENCE_KINDS
from quant.engines.alpha.strategies.risk_tree import validate_share

HEADER = (
    "# Global settings. sizing, trade_setup and live are set through the dashboard API (owner sign-in).\n"
    "# Other keys are edited by hand.\n"
)
DEFAULT_RISK = 0.0025
RISK_MAX = 0.05          # same upper bound as RiskLimits.risk_per_trade
SYMBOLS_MIN, SYMBOLS_MAX = 1, 100
KEYS = ("risk_per_trade", "max_symbols_per_portfolio", "strategy", "strategies",
        "live_max_capital_inr", "brokerage",
        "htf_interval", "mtf_interval", "ltf_interval",
        "capital_inr", "max_daily_loss_pct", "max_strategy_loss_pct", "aggressive_share",
        "mtf_borrowed_inr")
# Loss budgets as a share of capital (owner 2026-11-19). Saved in settings.yaml risk_budget;
# the rupee caps in risk_limits.yaml are derived from them on every save.
BUDGET_KEYS = ("capital_inr", "max_daily_loss_pct", "max_strategy_loss_pct")
BUDGET_MAX_PCT = 0.10  # a loss budget above 10 % of capital is refused
# Rupee caps owned by the Trade setup budget. /api/limits refuses them so they cannot drift.
BUDGET_OWNED_LIMITS = ("max_daily_loss", "max_strategy_loss")
SETUP_LIMIT_KEYS = ("max_symbols_per_portfolio", *BUDGET_OWNED_LIMITS)
LIVE_KEYS = ("live_max_capital_inr",)
# Timeframe labels, same style as the OCL params (htf_interval, ltf_interval in config/bot.yaml).
TIMEFRAMES = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60, "75m": 75,
              "125m": 125, "1d": 1440, "1w": 10080, "1M": 43200}  # weekly and monthly added 2026-11-22 (minutes approximate for ordering)
TF_KEYS = {"htf_interval": "htf", "mtf_interval": "mtf", "ltf_interval": "ltf"}  # API key -> yaml key
# Strategies the bot can run (owner_patterns.SEQUENCE_KINDS). The dashboard offers exactly these.
BOT_STRATEGIES = tuple(SEQUENCE_KINDS)
BOT_CONFIG_NAME = "bot.yaml"  # same folder as settings.yaml; the bot reads its `strategies:` list


def bot_config_path(settings_path) -> Path:
    """bot.yaml sits beside settings.yaml in config/."""
    return Path(settings_path).parent / BOT_CONFIG_NAME


def read_bot_strategies(bot_path) -> List[str]:
    """Names in the `strategies:` list of bot.yaml. Missing file or list gives an empty list."""
    path = Path(bot_path)
    if not path.is_file():
        return []
    names: List[str] = []
    inside = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("strategies:"):
            inside = True
            continue
        if inside:
            if line.startswith("  - "):
                names.append(line[4:].split("#", 1)[0].strip())
            elif line.strip() and not line.startswith(" "):
                break
    return names


def set_bot_strategies(bot_path, names: List[str]) -> None:
    """Replace the `strategies:` list in bot.yaml. Comments and other keys are kept as written.

    Atomic write. The bot reads this list when it starts, so a change applies at the next start.
    """
    path = Path(bot_path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    new_block = [f"  - {name}\n" for name in names]
    start = next((i for i, ln in enumerate(lines) if ln.startswith("strategies:")), None)
    if start is None:
        raise ValueError("bot.yaml has no strategies: list to update")
    end = start + 1
    while end < len(lines) and lines[end].startswith("  - "):
        end += 1
    lines[start + 1:end] = new_block
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(tmp, path)


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
    budget = data.get("risk_budget") or {}
    limits_obj = RiskLimits.from_yaml(limits_path) if Path(limits_path).exists() else None
    return {
        "risk_per_trade": risk,
        "aggressive_share": float((data.get("cycle") or {}).get("aggressive_share", 0.60)),
        "max_symbols_per_portfolio": symbols,
        "strategy": trade.get("strategy"),
        "strategies": strategy_names(strategies_dir),
        "bot_strategies": read_bot_strategies(bot_config_path(settings_path)),
        "available_bot_strategies": list(BOT_STRATEGIES),
        "live_max_capital_inr": live.get("max_capital_inr"),
        "brokerage": brokerage if isinstance(brokerage, dict) else None,
        "brokerage_example": summary,
        "htf_interval": tf.get("htf"),
        "mtf_interval": tf.get("mtf"),
        "ltf_interval": tf.get("ltf"),
        "timeframe_options": list(TIMEFRAMES),
        "capital_inr": budget.get("capital_inr"),
        "max_daily_loss_pct": budget.get("max_daily_loss_pct"),
        "max_strategy_loss_pct": budget.get("max_strategy_loss_pct"),
        "max_daily_loss": limits_obj.max_daily_loss if limits_obj else None,
        "max_strategy_loss": limits_obj.max_strategy_loss if limits_obj else None,
        "mtf_borrowed_inr": (data.get("margin") or {}).get("mtf_borrowed_inr"),
    }


def load_mtf_enabled(settings_path) -> bool:
    """Margin funding switch (margin.mtf_enabled). Only the boolean true turns it on; anything else is off."""
    value = (_load(Path(settings_path)).get("margin") or {}).get("mtf_enabled")
    return value is True


def load_mtf_borrowed(settings_path) -> float:
    """Saved MTF (margin trading facility) borrowed amount in rupees. Missing or bad values give 0."""
    value = (_load(Path(settings_path)).get("margin") or {}).get("mtf_borrowed_inr")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return 0.0
    return float(value)


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
        elif key == "capital_inr":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or value <= 0:
                raise ValueError("capital_inr must be a positive number of rupees")
            clean[key] = float(value)
        elif key in ("max_daily_loss_pct", "max_strategy_loss_pct"):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or not 0 < value <= BUDGET_MAX_PCT:
                raise ValueError(f"{key} must be a fraction in (0, {BUDGET_MAX_PCT}], e.g. 0.02 for 2 %")
            clean[key] = float(value)
        elif key == "mtf_borrowed_inr":
            if value is None:
                clean[key] = 0.0
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or value < 0:
                raise ValueError("mtf_borrowed_inr must be zero or a positive number of rupees")
            else:
                clean[key] = float(value)
        elif key == "aggressive_share":
            # Owner tree (2026-11-20; floor lowered to 30 % on 2026-11-26): 30 % to 80 % only. Refused outside, never clamped.
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("aggressive_share must be a fraction between 0.30 and 0.80, e.g. 0.60 for 60 %")
            clean[key] = validate_share(value)
        elif key == "brokerage":
            clean[key] = broker_costs.validate_brokerage(value)
        elif key in TF_KEYS:  # null clears the timeframe
            if value is not None and (not isinstance(value, str) or value not in TIMEFRAMES):
                raise ValueError(f"{key} must be one of: {', '.join(TIMEFRAMES)}")
            clean[key] = value
        elif key == "strategies":
            if not isinstance(value, list) or not value:
                raise ValueError("choose at least one strategy")
            if len(set(value)) != len(value):
                raise ValueError("a strategy is listed twice")
            for name in value:
                if name not in BOT_STRATEGIES:
                    raise ValueError(f"unknown strategy: {name!r}. Choose from: {', '.join(BOT_STRATEGIES)}")
            clean[key] = list(value)
        else:  # strategy (legacy single choice, saved only)
            if value not in strategies:
                raise ValueError(f"unknown strategy: {value!r}. Choose one of: {', '.join(strategies) or 'none'}")
            clean[key] = value
    return clean


def _budget_plan(current_budget: Mapping[str, Any], clean: Mapping[str, Any]) -> Dict[str, Any]:
    """Merged budget (stored values + this save) and the rupee caps it implies. Refuses without a capital base."""
    merged = {k: clean.get(k, current_budget.get(k)) for k in BUDGET_KEYS}
    if merged["capital_inr"] is None:
        raise ValueError("set capital_inr first: the loss caps are a percent of capital")
    rupees: Dict[str, float] = {}
    if merged["max_daily_loss_pct"] is not None:
        rupees["max_daily_loss"] = round(float(merged["max_daily_loss_pct"]) * float(merged["capital_inr"]), 2)
    if merged["max_strategy_loss_pct"] is not None:
        rupees["max_strategy_loss"] = round(float(merged["max_strategy_loss_pct"]) * float(merged["capital_inr"]), 2)
    return {"merged": merged, "rupees": rupees}


def apply_setup_updates(settings_path, strategies_dir, limits_path,
                        updates: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Save validated updates. Returns {"old": {...}, "new": {...}} for the changed keys.

    Loss budgets (% of capital) are written to settings.yaml risk_budget, and the rupee caps they
    imply are written to risk_limits.yaml in the same call. The rupee caps are never set directly.
    """
    settings = Path(settings_path)
    clean = validate_setup_updates(updates, strategy_names(strategies_dir))
    old: Dict[str, Any] = {}
    new_extra: Dict[str, Any] = {}
    current = _load(settings)
    budget_keys_in = set(BUDGET_KEYS) & set(clean)
    plan = None
    if budget_keys_in:
        plan = _budget_plan(current.get("risk_budget") or {}, clean)  # refuse before anything is written
    if "max_symbols_per_portfolio" in clean:
        limit_result = apply_limit_updates(limits_path, {"max_symbols_per_portfolio": clean["max_symbols_per_portfolio"]})
        old["max_symbols_per_portfolio"] = limit_result["old"]["max_symbols_per_portfolio"]
    if plan is not None:
        if plan["rupees"]:
            rupee_result = apply_limit_updates(limits_path, plan["rupees"])
            old.update(rupee_result["old"])
            new_extra.update(rupee_result["new"])
        for k in BUDGET_KEYS:
            if k in clean:
                old[k] = (current.get("risk_budget") or {}).get(k)
    if "strategies" in clean:
        bot_path = bot_config_path(settings)
        old["strategies"] = read_bot_strategies(bot_path)
        set_bot_strategies(bot_path, clean["strategies"])
    file_keys = {"risk_per_trade", "strategy", "brokerage", "aggressive_share", "mtf_borrowed_inr",
                 *LIVE_KEYS, *TF_KEYS}
    if file_keys & set(clean) or plan is not None:
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
        if "aggressive_share" in clean:
            cycle_sec = dict(current.get("cycle") or {})
            old["aggressive_share"] = cycle_sec.get("aggressive_share")
            cycle_sec["aggressive_share"] = clean["aggressive_share"]
            current["cycle"] = cycle_sec
        if "mtf_borrowed_inr" in clean:
            margin = dict(current.get("margin") or {})
            old["mtf_borrowed_inr"] = margin.get("mtf_borrowed_inr")
            margin["mtf_borrowed_inr"] = clean["mtf_borrowed_inr"]
            current["margin"] = margin
        if "live_max_capital_inr" in clean:
            old["live_max_capital_inr"] = live.get("max_capital_inr")
            live["max_capital_inr"] = clean["live_max_capital_inr"]
        current["sizing"] = sizing
        current["trade_setup"] = trade
        current["live"] = live
        if plan is not None:
            current["risk_budget"] = {k: plan["merged"][k] for k in BUDGET_KEYS if plan["merged"][k] is not None}
        body = HEADER + yaml.safe_dump(current, sort_keys=False)
        tmp = settings.with_name(settings.name + ".tmp")
        settings.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, settings)
    return {"old": old, "new": {**clean, **new_extra}}
