"""Append-only log of every tested variant. DSR needs the true trial count.

Flow stage: Trial logger + DSR/PBO
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/auto_tune.py
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

    def _rows(self, kind: str, strategy_id: Optional[str]) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        # Rows without "kind" are plain trials (older format from record()).
        return [r for r in rows
                if r.get("kind", "trial") == kind
                and (strategy_id is None or r["strategy_id"] == strategy_id)]

    def trials(self, strategy_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._rows("trial", strategy_id)

    def runs(self, strategy_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """One ``optimizer_run`` row per optimisation run (sampler, bounds, folds, cost)."""
        return self._rows("optimizer_run", strategy_id)

    def count(self, strategy_id: Optional[str] = None) -> int:
        """True trial count for DSR: every variant tried, failed ones included."""
        return len(self.trials(strategy_id))

    def sharpe_variance(self, strategy_id: Optional[str] = None) -> float:
        # Failed trials have sharpe None: they count in count() but not in the variance.
        values = [t["sharpe"] for t in self.trials(strategy_id) if t.get("sharpe") is not None]
        return float(np.var(values, ddof=1)) if len(values) >= 2 else 0.0
