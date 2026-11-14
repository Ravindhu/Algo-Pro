"""Strategy health: rolling Sharpe and a one-sided CUSUM drift alarm that can auto-disable a strategy.

Flow stage: Strategy health
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/live_monitor.py
"""
from __future__ import annotations

from typing import Sequence

import numpy as np


def rolling_sharpe(returns: Sequence[float], window: int = 60, periods: int = 252) -> np.ndarray:
    r = np.asarray(returns, float)
    out = np.full(len(r), np.nan)
    for i in range(window, len(r) + 1):
        w = r[i - window:i]
        s = w.std(ddof=1)
        out[i - 1] = np.nan if s == 0 else w.mean() / s * np.sqrt(periods)
    return out


def cusum_alarm(returns: Sequence[float], mu0: float, k: float, h: float) -> bool:
    """Lower CUSUM: alarm when the mean falls below mu0 by more than k, accumulated past h."""
    s = 0.0
    for x in returns:
        s = min(0.0, s + (x - (mu0 - k)))
        if s < -h:
            return True
    return False


def emit_deviation_alert(alerts, component: str, returns: Sequence[float], mu0: float, k: float, h: float) -> bool:
    """Run the CUSUM drift alarm and emit an ERROR alert when it fires (flow item 22).

    `alerts` is any object with emit(level, component, message), e.g. notify.alerts.Alerts.
    """
    fired = cusum_alarm(returns, mu0, k, h)
    if fired:
        alerts.emit("ERROR", component, "strategy deviation: CUSUM drift alarm fired")
    return fired
