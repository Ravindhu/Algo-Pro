"""1-minute scanner for the screened symbols, PAPER only. Signal -> quantity -> gate -> POV slicer.

Flow stage: Strategy scan, Sizing input, Risk gate, Algo router (POV slicer)
Status: PHASE 2 IMPLEMENTED (paper only, 2026-11-08; not live). No broker call is possible from here:
the only order sink is PaperSender, which records fills in memory.

One cycle (called every 1 minute by the runner):
  1. Session check: only trade in the 'open' state of the NSE session, and never on a skip date
     (owner, 2026-11-08: skip the Muhurat session on 2026-11-08).
  2. For each screened symbol, run every BUY strategy on its closed bars.
  3. A signal is handled once per (strategy, symbol, signal bar). The key is saved before any action,
     so a restart cannot trade the same bar twice.
  4. Quantity from portfolio construction (quant.portfolio.paper_qty.max_entry_qty).
  5. The parent intent goes to PovExecutor, which slices it into child intents. Each child goes through
     the risk gate, the approved queue, and PaperSender.

Known gap (paper): there are no simulated exits yet. A strategy that trades stays 'in trade' and sends
no new signal for that symbol. This is conservative: one open paper position per strategy and symbol.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set

import pandas as pd

from quant.core.config import RiskLimits
from quant.core.session_clock import market_session_state
from quant.core.types import GateDecision, TradeIntent
from quant.execution.pov_executor import PovExecutor
from quant.portfolio.paper_qty import max_entry_qty
from quant.portfolio.tiers import TierTable
from quant.strategies.base import MultiTimeframeStrategy

PAPER_KEY = "PAPER"                      # PovExecutor needs a non-empty key; paper never uses it
MAX_CHILDREN_PER_SIGNAL = 20             # safety cap on POV slices for one parent in one cycle


class SignalLedger:
    """Keys of signal bars already handled. Saved to a JSON file when a path is given."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path else None
        self._keys: Set[str] = set()
        if self._path and self._path.exists():
            self._keys = set(json.loads(self._path.read_text(encoding="utf-8")))

    def seen(self, key: str) -> bool:
        return key in self._keys

    def mark(self, key: str) -> None:
        self._keys.add(key)
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(sorted(self._keys)), encoding="utf-8")


class ListAudit:
    """In-memory audit sink (AuditSink protocol). The file-based sink comes with the runner."""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    def append(self, kind: str, payload: Dict) -> str:
        self.records.append({"kind": kind, **payload})
        return str(len(self.records))


class PaperSender:
    """ChildSender that records fills. It fills at the intent's reference price and never calls a broker."""

    def __init__(self) -> None:
        self.fills: List[Dict[str, Any]] = []

    def place_child(self, intent: TradeIntent, api_key: str, decision: GateDecision,
                    price_type: str = "MARKET", limit_price: Optional[float] = None) -> Dict:
        if not decision.approved:
            raise PermissionError("paper sender only takes gate-approved intents")
        fill = {"intent_id": intent.intent_id, "symbol": intent.symbol, "side": intent.side,
                "qty": intent.qty, "price": intent.ref_price, "mode": "PAPER"}
        self.fills.append(fill)
        return fill


def session_allows_entries(now: datetime, calendar: Any, skip_dates: Iterable, entry_cutoff: str) -> tuple:
    """(allowed, reason). Entries only in the 'open' state and never on a skip date."""
    if now.date() in set(skip_dates):
        return False, f"{now.date().isoformat()} is a skip date (owner decision)"
    state = market_session_state(now, entry_cutoff=entry_cutoff, calendar=calendar)
    if state["state"] != "open":
        return False, f"session state {state['state']}: {state['reason']}"
    return True, "open"


@dataclass
class Scanner:
    strategies: Mapping[str, List[MultiTimeframeStrategy]]     # symbol -> strategy instances for it
    executor: PovExecutor
    ledger: SignalLedger
    audit: Any
    capital: float
    risk_fraction: float
    limits: RiskLimits
    tiers: TierTable
    calendar: Any
    skip_dates: Iterable = field(default_factory=tuple)
    entry_cutoff: str = "14:45"
    # Average daily volume in shares per symbol. Supplied by the runner from daily data. A symbol with
    # no ADV is rejected by the gate (fail closed: "ADV unknown").
    adv_shares: Mapping[str, float] = field(default_factory=dict)

    def cycle(self, now: datetime, frames_by_symbol: Mapping[str, Mapping[str, pd.DataFrame]],
              open_notional: float = 0.0) -> List[Dict[str, Any]]:
        """Run one scan. Returns the audit records written in this cycle."""
        start = len(self.audit.records) if hasattr(self.audit, "records") else 0
        allowed, why = session_allows_entries(now, self.calendar, self.skip_dates, self.entry_cutoff)
        if not allowed:
            self.audit.append("cycle_skipped", {"at": now.isoformat(), "reason": why})
            return self._new(start)

        for symbol, frames in frames_by_symbol.items():
            for strat in self.strategies.get(symbol, []):
                sig = strat.signal_frames(frames)
                if sig is None:
                    continue
                self._handle(strat, symbol, sig, frames, now, open_notional)
        return self._new(start)

    def _new(self, start: int) -> List[Dict[str, Any]]:
        return list(self.audit.records[start:]) if hasattr(self.audit, "records") else []

    def _handle(self, strat: MultiTimeframeStrategy, symbol: str, sig: Dict[str, Any],
                frames: Mapping[str, pd.DataFrame], now: datetime, open_notional: float) -> None:
        key = f"{strat.name}|{symbol}|{sig['time']}"
        if self.ledger.seen(key):
            return
        self.ledger.mark(key)                              # before any action: restart-safe

        self.audit.append("signal", {"key": key, "strategy": strat.name, "symbol": symbol,
                                     "entry": sig["entry_ref"], "stop": sig["sl_price"],
                                     "exit_mode": sig["exit_mode"], "time": sig["time"]})
        qd = max_entry_qty(entry=float(sig["entry_ref"]), stop=float(sig["sl_price"]),
                           capital=self.capital, risk_fraction=self.risk_fraction,
                           limits=self.limits, open_notional=open_notional, tiers=self.tiers)
        self.audit.append("qty", {"key": key, **qd.as_audit_record()})
        if qd.qty < 1:
            self.audit.append("skipped", {"key": key, "reasons": list(qd.reasons) or ["qty 0"]})
            return

        bar_volume = float(frames[strat.ltf_tf]["volume"].iloc[-2]) if "volume" in frames[strat.ltf_tf] else 0.0
        parent = TradeIntent(intent_id=key, strategy_id=strat.name, symbol=symbol, side="BUY",
                             qty=qd.qty, ref_price=float(sig["entry_ref"]), kind="entry",
                             lot_size=1, adv_shares=self.adv_shares.get(symbol), trade_day=now.date())
        remaining, seq, sent_any = parent.qty, 1, False
        while remaining > 0 and seq <= MAX_CHILDREN_PER_SIGNAL:
            child_q = self.executor.child_qty(remaining, bar_volume)
            if child_q < 1:
                self.audit.append("slice_stopped", {"key": key, "remaining": remaining,
                                                    "reason": "no volume in last closed bar (POV slice 0)"})
                break
            child = PovExecutor.make_child(parent, child_q, seq)
            outcome = self.executor.submit_child(child)
            if not outcome.sent:
                self.audit.append("slice_stopped", {"key": key, "remaining": remaining,
                                                    "reasons": list(outcome.reasons)})
                break
            sent_any = True
            remaining -= child_q
            seq += 1
        if sent_any:
            strat.on_entry()                               # blocks new signals for this symbol until exits exist
