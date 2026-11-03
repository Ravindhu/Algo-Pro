"""Risk limits model and YAML loader. Unset limits are None and fail closed.

Flow stage: Risk gate
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/config_loader.py
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

    @classmethod
    def from_yaml(cls, path) -> "RiskLimits":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        names = {f.name for f in fields(cls)}
        unknown = set(data) - names
        if unknown:
            raise ValueError(f"unknown risk keys: {sorted(unknown)}")
        return cls(**{k: v for k, v in data.items() if v is not None})

    def unset(self) -> List[str]:
        """Names of numeric limits that are still None."""
        return [f.name for f in fields(self) if f.name != "blocked" and getattr(self, f.name) is None]
