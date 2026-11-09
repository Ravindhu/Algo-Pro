"""Risk limits model and YAML loader. Unset limits are None and fail closed.

Owner decision 0031 (2026-11-09): the four money limits are set in PERCENT of capital.
The YAML holds only `capital_inr` (one number) and percentage keys. Rupee values are
computed here once, at load time, and the risk gate keeps using the rupee fields.
A rupee key in the YAML is an error, so a rupee limit cannot come back by mistake.

Flow stage: Risk gate
Status: PHASE 1 IMPLEMENTED. Percent limits added 2026-11-09 (decision 0031).
Existing code to reuse or wrap (do not copy): Blue_engine/core/config_loader.py
"""
from __future__ import annotations

import math
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml

# Percentage key -> the rupee field it fills at load time (fraction, 0.005 = 0.5%).
PERCENT_TO_RUPEE: Dict[str, str] = {
    "max_daily_loss_pct": "max_daily_loss",
    "max_strategy_loss_pct": "max_strategy_loss",
    "max_order_notional_pct": "max_order_notional",
    "max_open_exposure_pct": "max_open_exposure",
}
RUPEE_KEYS = tuple(PERCENT_TO_RUPEE.values())


@dataclass(frozen=True)
class RiskLimits:
    blocked: bool = False
    max_order_quantity: Optional[int] = None
    max_adv_pct: Optional[float] = None  # fraction of ADV, 0.01 = 1%
    max_symbols_per_portfolio: Optional[int] = None  # distinct symbols held; null fails closed for new symbols
    capital_inr: Optional[float] = None  # capital base for the percent limits (INR)
    max_daily_loss_pct: Optional[float] = None  # fraction of capital, 0.005 = 0.5%
    max_strategy_loss_pct: Optional[float] = None
    max_order_notional_pct: Optional[float] = None
    max_open_exposure_pct: Optional[float] = None
    # Rupee fields below are computed from capital_inr x pct at load time. Not set in YAML.
    max_order_notional: Optional[float] = None
    max_open_exposure: Optional[float] = None
    max_daily_loss: Optional[float] = None
    max_strategy_loss: Optional[float] = None

    @classmethod
    def from_yaml(cls, path) -> "RiskLimits":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.from_mapping(data)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "RiskLimits":
        """Build limits from YAML-style data. Percent keys are turned into rupee fields here."""
        names = {f.name for f in fields(cls)}
        unknown = set(data) - names
        if unknown:
            raise ValueError(f"unknown risk keys: {sorted(unknown)}")
        given = {k: v for k, v in data.items() if v is not None}
        rupee = sorted(k for k in RUPEE_KEYS if k in given)
        if rupee:
            raise ValueError(f"rupee limits are not allowed, use the percent keys instead: {rupee}")
        for key in ("max_daily_loss_pct", "max_strategy_loss_pct", "max_order_notional_pct", "max_open_exposure_pct"):
            if key in given:
                value = given[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                        or not 0 < value <= 1:
                    raise ValueError(f"{key} must be a fraction in (0, 1], got {value!r}")
        capital = given.get("capital_inr")
        if capital is not None:
            if isinstance(capital, bool) or not isinstance(capital, (int, float)) or not math.isfinite(capital) \
                    or capital <= 0:
                raise ValueError(f"capital_inr must be a positive number, got {capital!r}")
        computed: Dict[str, float] = {}
        if capital is not None:
            for pct_key, rupee_key in PERCENT_TO_RUPEE.items():
                if pct_key in given:
                    computed[rupee_key] = float(capital) * float(given[pct_key])
        # A pct key with no capital_inr leaves the rupee field None, so the gate fails closed.
        return cls(**{**given, **computed})

    @staticmethod
    def risk_per_trade(path=None, default: float = 0.0025) -> float:
        """Risk per trade as a share of equity from settings.yaml (sizing). Default 0.25%.

        Missing file or key gives the default. Values outside (0, 0.05] raise ValueError.
        """
        import yaml as _yaml

        if path is None or not Path(path).exists():
            return default
        data = _yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        value = (data.get("sizing") or {}).get("risk_per_trade", default)
        if not isinstance(value, (int, float)) or not 0 < float(value) <= 0.05:
            raise ValueError(f"sizing.risk_per_trade must be in (0, 0.05], got {value!r}")
        return float(value)

    def unset(self) -> List[str]:
        """Names of numeric limits that are still None."""
        return [f.name for f in fields(self) if f.name != "blocked" and getattr(self, f.name) is None]
