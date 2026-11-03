"""Append-only log of every tested variant. DSR needs the true trial count.

Flow stage: Trial logger + DSR/PBO
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/auto_tune.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np


class TrialLog:
    def __init__(self, path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, strategy_id: str, params: Dict[str, Any], sharpe: float, n_obs: int) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "strategy_id": strategy_id,
            "params": params,
            "sharpe": float(sharpe),
            "n_obs": int(n_obs),
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")

    def trials(self, strategy_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        return [r for r in rows if strategy_id is None or r["strategy_id"] == strategy_id]

    def count(self, strategy_id: Optional[str] = None) -> int:
        return len(self.trials(strategy_id))

    def sharpe_variance(self, strategy_id: Optional[str] = None) -> float:
        values = [t["sharpe"] for t in self.trials(strategy_id)]
        return float(np.var(values, ddof=1)) if len(values) >= 2 else 0.0
