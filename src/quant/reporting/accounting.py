"""Authorized accounting view and FIFO tax lots (STCG vs LTCG by holding period). Rates are not set here.

Flow stage: Dashboard (accounting view), Tax lots
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/jobs/daily_portfolio.py
"""
from __future__ import annotations

from collections import deque
from datetime import date
from typing import Deque, Dict, List, Tuple

LTCG_DAYS = 365  # holding period threshold for listed equity. Confirm current rule with a tax adviser.


def require_role(role: str, allowed: Tuple[str, ...] = ("accountant", "owner")) -> None:
    if role not in allowed:
        raise PermissionError("accounting view needs an authorised role")


def fifo_realised(buys: List[Tuple[date, float, float]], sells: List[Tuple[date, float, float]]) -> List[Dict]:
    """buys/sells: (date, qty, price). Sells match the oldest open lots first."""
    lots: Deque[list] = deque([d, q, p] for d, q, p in buys)
    out = []
    for sd, sq, sp in sells:
        remaining = sq
        while remaining > 0 and lots:
            bd, bq, bp = lots[0]
            take = min(bq, remaining)
            out.append({"buy_date": bd, "sell_date": sd, "qty": take,
                        "pnl": take * (sp - bp),
                        "term": "LTCG" if (sd - bd).days > LTCG_DAYS else "STCG"})
            remaining -= take
            lots[0][1] -= take
            if lots[0][1] == 0:
                lots.popleft()
        if remaining > 0:
            raise ValueError("sell exceeds open lots")
    return out
