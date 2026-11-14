"""
config_loader.py - parse the master JSON config.

Schema (v2, "toggle" format):

    {
      "strategy_label": "<tag for OpenAlgo orderbook>",
      "api_key": null,
      "host":    null,
      "ws_url":  null,

      "symbol":   "INFY",
      "exchange": "NSE",

      "strategies": {
        "keylevel": true,    # each strategy is enabled with a boolean
        "pov":      true
      },

      "keylevel": { ...keylevel params... },
      "pov":      { ...pov params... }
    }

Rules enforced by parse_master_config()
----------------------------------------
- `strategies` is a dict of {name: bool}. Only `true` entries run.
- `pov` is the slicer. If any signal is enabled, pov must also be enabled.
- Each enabled strategy MUST have a matching top-level config block.
- Top-level `symbol` and `exchange` are shared by all strategies.
- If no signal is enabled, the slicer runs immediately using its own
  `action` and `quantity` (replaces the old "manual" signal).
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple


class ConfigError(ValueError):
    """Raised when a master config is invalid."""


def parse_master_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the master config and return a normalised view.

    Returns a dict with keys:
        strategy_label, symbol, exchange, api_key, host, ws_url,
        active_signals:  list of (name, params_dict)
        active_slicer:   (name, params_dict) or None

    Raises:
        ConfigError: with a human-readable message on any problem.
    """
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")

    # Top-level required fields
    for key in ("symbol", "exchange", "strategies"):
        if key not in raw:
            raise ConfigError(f"missing required top-level key: '{key}'")

    strategies = raw["strategies"]
    if not isinstance(strategies, dict):
        raise ConfigError("'strategies' must be an object of {name: true|false}")

    symbol = str(raw["symbol"]).strip()
    exchange = str(raw["exchange"]).strip().upper()
    if not symbol or not exchange:
        raise ConfigError("'symbol' and 'exchange' must be non-empty")

    # Resolve which signals/slicer are active
    active_signals: List[Tuple[str, Dict[str, Any]]] = []
    active_slicer: Tuple[str, Dict[str, Any]] | None = None

    for name, enabled in strategies.items():
        if not enabled:
            continue
        if name not in raw or not isinstance(raw[name], dict):
            raise ConfigError(
                f"strategy '{name}' is enabled but has no config block "
                f"at the top level"
            )
        params = raw[name]
        if name == "pov":
            if active_slicer is not None:
                raise ConfigError("only one slicer may be enabled at a time")
            active_slicer = (name, params)
        else:
            active_signals.append((name, params))

    # POV is required when any signal is enabled
    if active_signals and active_slicer is None:
        raise ConfigError(
            f"signals {[n for n, _ in active_signals]} are enabled but no "
            f"slicer (set 'pov: true' under 'strategies')"
        )

    # Merge top-level symbol/exchange into each strategy's params so the
    # signal/slicer constructors don't need to look at two places. Work on
    # copies so the caller's input dict is never mutated (repeated runs and
    # tests must not become order-dependent).
    merged_signals: List[Tuple[str, Dict[str, Any]]] = []
    for name, params in active_signals:
        merged = dict(params)
        merged.setdefault("symbol", symbol)
        merged.setdefault("exchange", exchange)
        merged_signals.append((name, merged))
    active_signals = merged_signals
    if active_slicer is not None:
        _name, params = active_slicer
        merged = dict(params)
        merged.setdefault("symbol", symbol)
        merged.setdefault("exchange", exchange)
        merged.setdefault("strategy_label", raw.get("strategy_label", "POV"))
        active_slicer = (_name, merged)

    return {
        "strategy_label": raw.get("strategy_label", "POV"),
        "api_key":        raw.get("api_key"),
        "host":           raw.get("host"),
        "ws_url":         raw.get("ws_url"),
        "symbol":         symbol,
        "exchange":       exchange,
        "active_signals": active_signals,
        "active_slicer":  active_slicer,
    }
