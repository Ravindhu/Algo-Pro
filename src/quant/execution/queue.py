"""Approved order queue. Accepts only intents with an approved gate decision.

Flow stage: Approved order queue
Status: PHASE 1 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/core/orchestrator.py
"""
from __future__ import annotations

from collections import deque
from typing import Deque

from quant.core.types import GateDecision, TradeIntent


class ApprovedOrderQueue:
    def __init__(self) -> None:
        self._items: Deque[TradeIntent] = deque()

    def push(self, intent: TradeIntent, decision: GateDecision) -> None:
        if not isinstance(decision, GateDecision) or not decision.approved:
            raise PermissionError("only gate-approved intents can be queued")
        if decision.intent_id != intent.intent_id:
            raise PermissionError("decision does not match intent")
        self._items.append(intent)

    def pop(self) -> TradeIntent:
        return self._items.popleft()

    def __len__(self) -> int:
        return len(self._items)
