"""Point-in-time feature store: a value is visible only from its own timestamp onward.

Flow stage: Feature store
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/ml/features/point_in_time.py
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from typing import Any, Dict, List, Tuple


class FeatureStore:
    def __init__(self) -> None:
        self._series: Dict[Tuple[str, str], Tuple[List[Any], List[float]]] = defaultdict(lambda: ([], []))

    def put(self, feature: str, symbol: str, ts, value: float) -> None:
        times, values = self._series[(feature, symbol)]
        i = bisect.bisect_right(times, ts)
        times.insert(i, ts)
        values.insert(i, float(value))

    def get_asof(self, feature: str, symbol: str, ts):
        """Latest value known at or before ts. None if nothing was known yet."""
        times, values = self._series.get((feature, symbol), ([], []))
        i = bisect.bisect_right(times, ts)
        return values[i - 1] if i else None
