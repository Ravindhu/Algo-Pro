"""Square-off for MIS positions and futures rollover before expiry.

Flow stage: Square-off and rollover
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/auction_windows.py
NOTE: the default cutoff is a placeholder. Set it to your broker's MIS cutoff in config.
"""
from __future__ import annotations

from datetime import date, time


def should_square_off(now: time, product: str, cutoff: time = time(15, 15)) -> bool:
    return product == "MIS" and now >= cutoff


def roll_due(expiry: date, today: date, roll_days: int = 3) -> bool:
    return (expiry - today).days <= roll_days
