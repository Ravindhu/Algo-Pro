"""Broker session manager: daily login and token age check.

Flow stage: Broker session manager
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/broker_auth.py
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


class BrokerSession:
    def __init__(self, max_age: timedelta = timedelta(hours=20)) -> None:
        self.max_age = max_age
        self.issued_at: Optional[datetime] = None

    def record_login(self, now: datetime) -> None:
        self.issued_at = now

    def needs_login(self, now: datetime) -> bool:
        return self.issued_at is None or now - self.issued_at > self.max_age
