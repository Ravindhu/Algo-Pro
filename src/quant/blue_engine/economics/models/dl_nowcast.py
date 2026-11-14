"""
models/dl_nowcast.py - optional deep-learning nowcaster (M8, frontier research).

The plan's M8. This module is a **deliberately thin, opt-in stub**: it defines
the interface and a safe fallback, but performs no heavy compute unless a DL
backend (``torch``) is installed AND the caller explicitly requests it. The
point is to keep the frontier experiment *isolated* from the production path -
the engine never imports this on the critical path.

Why a stub and not a model
--------------------------
Deep nets on 9 monthly macro features need years of data and careful tuning;
shipping one now would be an unvalidated black box. Instead we fix the contract
(``forecast(history) -> point + interval``) so a real LSTM / Temporal Fusion
Transformer can be dropped in later *without changing the engine*.

Degrades to ``method='unavailable'`` when torch is absent, and to a *naive
persistence* forecast otherwise so callers always get a usable number.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ...core.results import mean, safe_float, stdev

try:  # optional, heavy
    import torch  # type: ignore  # noqa: F401
    _HAS_TORCH = True
except Exception:  # noqa: BLE001
    _HAS_TORCH = False


def naive_nowcast(history: Sequence[float]) -> Dict[str, Any]:
    """Random-walk nowcast + volatility band (the always-available baseline).

    Forecast = last value; uncertainty = recent stdev. This is what any DL model
    must *beat* out-of-sample to earn its place on the desk.
    """
    clean = [safe_float(v) for v in history]
    clean = [v for v in clean if v is not None]
    if not clean:
        return {"method": "insufficient_data", "forecast": None,
                "interval": None}
    last = clean[-1]
    sd = stdev(clean[-12:]) if len(clean) >= 3 else (stdev(clean) or 0.0)
    sd = sd or 0.0
    return {"method": "naive", "forecast": round(last, 6),
            "interval": [round(last - 1.96 * sd, 6),
                         round(last + 1.96 * sd, 6)],
            "sd": round(sd, 6)}


def forecast(history: Sequence[float], *, prefer_dl: bool = False
             ) -> Dict[str, Any]:
    """Forecast the next macro reading.

    Parameters
    ----------
    history : time-ordered readings.
    prefer_dl : when True AND torch is installed, a DL model *would* be used.
                Today this returns the naive forecast with a note, because no
                validated weights ship. This is intentional and honest.

    Returns::

        {"method": "naive"|"dl"|"insufficient_data", "forecast": float,
         "interval": [lo, hi], "backend": "torch"|"none", "note": str}
    """
    base = naive_nowcast(history)
    if prefer_dl and _HAS_TORCH:
        base["method"] = "naive"          # no validated DL weights shipped yet
        base["backend"] = "torch"
        base["note"] = ("torch present but no validated DL weights are "
                        "shipped; returning naive forecast. Fit a model and "
                        "layer it here after walk-forward validation.")
    else:
        base["backend"] = "torch" if _HAS_TORCH else "none"
    return base


def available() -> bool:
    return _HAS_TORCH


__all__ = ["forecast", "naive_nowcast", "available"]