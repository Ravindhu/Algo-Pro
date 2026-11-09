"""Build the signals CSV for the paper runner from daily bars.

Flow stage: Signal, sizing (paper)
Status: PHASE 2 IMPLEMENTED (paper path, 2026-11-09). Feeds scripts/run_paper.py. Never calls the order API.

Steps on the decision date (the last date in the bars):
1. Strategy selects symbols from closes up to and including that date (no look-ahead).
2. Optimiser weights the selected symbols from their covariance over a trailing window.
3. Sizing: whole shares = floor(weight x capital / last close), capped by the per-order quantity
   and notional limits, and by the open-exposure limit (greedy by weight). The risk gate remains the
   final check; this stage never raises a limit.
4. Exits: a held symbol that is no longer selected gets a SELL exit intent for its held quantity.
5. If the optimiser fails, raise. Nothing is written (fail closed).

Intent IDs are deterministic (strategy, date, side, symbol), so a rerun does not resend intents
(run_paper skips seen IDs).
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from quant.portfolio.optimiser_registry import optimise
from quant.strategies.base import Strategy

ORDER_COLUMNS = ["intent_id", "strategy_id", "symbol", "side", "qty", "ref_price", "kind",
                 "lot_size", "prev_close", "circuit_pct", "adv_shares", "asm_gsm"]
PASS_THROUGH = ("prev_close", "circuit_pct", "adv_shares", "asm_gsm")


def _intent_id(strategy_id: str, day, side: str, symbol: str) -> str:
    return f"{strategy_id}-{pd.Timestamp(day):%Y%m%d}-{side}-{symbol}"


def build_signals(bars: pd.DataFrame, *, strategy: Strategy, optimiser_name: str,
                  strategy_id: str, limits: Dict[str, Any], capital: float,
                  held: Optional[Dict[str, int]] = None, cov_window: int = 60,
                  optimiser_config: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """Return the signals DataFrame (columns ORDER_COLUMNS) for the last date in ``bars``."""
    if capital <= 0:
        raise ValueError("capital must be positive")
    need = {"date", "symbol", "close"}
    if not need <= set(bars.columns):
        raise ValueError(f"bars missing columns: {sorted(need - set(bars.columns))}")

    close = bars.pivot(index="date", columns="symbol", values="close").sort_index()
    day = close.index[-1]
    held = {k: int(v) for k, v in (held or {}).items() if int(v) != 0}

    weights = strategy.target_weights(close)
    selected = [s for s in weights.index if float(weights[s]) > 0]
    rows = []
    last_px = close.loc[day]

    if selected:
        if len(selected) == 1:
            w = {selected[0]: 1.0}
        else:
            window = close[selected].iloc[-(cov_window + 1):]
            returns = window.pct_change().dropna()
            if len(returns) < 2:
                raise RuntimeError("not enough history for the covariance window")
            cov = returns.cov().to_numpy().tolist()
            problem = {"universe": selected, "cov": cov,
                       "vols": {s: float(np.sqrt(returns[s].var())) for s in selected}}
            out = optimise(optimiser_name, problem, config=optimiser_config)
            if not out["ok"]:
                raise RuntimeError(f"optimiser '{optimiser_name}' failed: {out['warnings']}")
            w = out["weights"]

        max_qty = float(limits.get("max_order_quantity") or 0)
        max_notional = float(limits.get("max_order_notional") or 0)
        max_exposure = float(limits.get("max_open_exposure") or 0)
        exposure = 0.0
        for sym in sorted(w, key=lambda s: -float(w[s])):
            px = float(last_px[sym])
            if not math.isfinite(px) or px <= 0 or float(w[sym]) <= 0:
                continue
            qty = math.floor(float(w[sym]) * capital / px)
            qty = min(qty, math.floor(max_qty), math.floor(max_notional / px))
            room = max_exposure - exposure
            qty = min(qty, math.floor(room / px))
            if qty <= 0:
                continue
            exposure += qty * px
            rows.append(_row(bars, day, strategy_id, sym, "BUY", qty, px, "entry"))

    target = {r["symbol"] for r in rows}
    for sym, qty in sorted(held.items()):
        if sym in target or qty <= 0:
            continue
        px = float(last_px.get(sym, float("nan")))
        if not math.isfinite(px) or px <= 0:
            raise RuntimeError(f"no close for held symbol {sym} on {pd.Timestamp(day).date()}")
        rows.append(_row(bars, day, strategy_id, sym, "SELL", qty, px, "exit"))

    return pd.DataFrame(rows, columns=ORDER_COLUMNS)


def _row(bars: pd.DataFrame, day, strategy_id: str, sym: str, side: str, qty: int,
         px: float, kind: str) -> Dict[str, Any]:
    last = bars[(bars["date"] == day) & (bars["symbol"] == sym)].iloc[-1]
    out: Dict[str, Any] = {
        "intent_id": _intent_id(strategy_id, day, side, sym),
        "strategy_id": strategy_id, "symbol": sym, "side": side, "qty": int(qty),
        "ref_price": float(px), "kind": kind, "lot_size": 1,
    }
    for col in PASS_THROUGH:
        out[col] = last[col] if col in bars.columns else None
    return out


def write_signals(signals: pd.DataFrame, path) -> None:
    """Write the CSV the paper runner reads (header always present, even when empty)."""
    signals.to_csv(path, index=False)
