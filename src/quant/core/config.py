"""Risk limits model and YAML loader. Unset limits are None and fail closed.

Flow stage: Risk gate
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/config_loader.py
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import List, Optional

import yaml


@dataclass(frozen=True)
class RiskLimits:
    blocked: bool = False
    max_order_quantity: Optional[int] = None
    max_order_notional: Optional[float] = None
    max_open_exposure: Optional[float] = None
    max_daily_loss: Optional[float] = None
    max_strategy_loss: Optional[float] = None
    max_adv_pct: Optional[float] = None
    max_symbols_per_portfolio: Optional[int] = None  # distinct symbols held; null fails closed for new symbols
    # Book-level caps (v5 2.6, added 2026-11-14). Null = unset = the pre-gate
    # refuses once the corresponding check is enabled.
    max_sector_pct: Optional[float] = None     # fraction of capital in one sector
    max_beta: Optional[float] = None           # |book beta| versus the index
    max_cvar: Optional[float] = None           # 99% CVaR as a fraction of capital
    max_drawdown: Optional[float] = None       # max drawdown as a fraction of capital

    @classmethod
    def from_yaml(cls, path) -> "RiskLimits":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        names = {f.name for f in fields(cls)}
        unknown = set(data) - names
        if unknown:
            raise ValueError(f"unknown risk keys: {sorted(unknown)}")
        return cls(**{k: v for k, v in data.items() if v is not None})

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

    #: Limits whose absence BLOCKS a live start. The book-level caps are
    #: deliberately excluded: a live run may begin without a sector cap the same
    #: way it may begin without a drawdown cap, because the pre-gate refuses those
    #: checks until the cap is set. Treating them as required would have made a
    #: live start impossible until every optional cap was filled in.
    REQUIRED_FOR_LIVE = (
        "max_order_quantity", "max_order_notional", "max_open_exposure",
        "max_daily_loss", "max_strategy_loss", "max_adv_pct",
        "max_symbols_per_portfolio",
    )

    def unset(self) -> List[str]:
        """Names of ALL numeric limits that are still None (for the dashboard)."""
        return [f.name for f in fields(self) if f.name != "blocked" and getattr(self, f.name) is None]

    def unset_required(self) -> List[str]:
        """The subset of ``unset()`` whose absence must block a live start."""
        return [name for name in self.REQUIRED_FOR_LIVE if getattr(self, name, None) is None]
