"""Paper runtime: 1-minute cycle that connects the bar feed, exits, the scanner and the gate.

Flow stage: Scanner runner (paper only, 2026-11-08; not live)
Status: PHASE 2 IMPLEMENTED (paper only; not live). No broker call is possible from this module.

One cycle at time `now` (called once per minute):
  1. Session check. Outside the NSE session (09:15 to 15:30 IST, weekdays, not holidays), nothing runs.
  2. Build frames per symbol from closed 1-minute bars (data/minute_feed.py). Stale data skips the symbol.
  3. Exits first: PaperPositionManager processes each new closed bar (trail, breakeven, target, stops).
     Each exit goes through the same executor as entries (risk gate, then PaperSender). A rejected exit
     stays pending and is retried every cycle.
  4. Entries: Scanner runs the BUY strategies, sizes with portfolio construction, passes the gate,
     and sends child orders through PovExecutor to PaperSender.
  5. State is saved to the state file (positions, strategy state, handoff marks), so a restart resumes.
The risk gate sees live state: open exposure, open symbols and available paper funds.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

import pandas as pd

from quant.core.config import RiskLimits
from quant.core.session_clock import market_session_state
from quant.data.minute_feed import StaleData, build_frames
from quant.execution.pov_executor import PovExecutor
from quant.monitor.safe_mode import SafeMode
from quant.portfolio.tiers import TierTable
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_switch import KillSwitch
from quant.strategies.base import MultiTimeframeStrategy
from quant.strategies.positions import PaperPositionManager
from quant.strategies.scanner import PAPER_KEY, ListAudit, PaperSender, Scanner, SignalLedger
from quant.monitor.broker_recon import ReconGate, startup_reconcile
from quant.strategies.state_store import load_state, restore_all, save_state

INTERVALS = ("5minute", "15minute")


@dataclass
class PaperRuntime:
    scanner: Scanner
    positions: PaperPositionManager
    sender: PaperSender
    audit: Any
    entry_cutoff: str = "14:45"
    state_path: Optional[Path] = None
    strategies: List[MultiTimeframeStrategy] = field(default_factory=list)
    handoff: Any = None
    recon: Optional[ReconGate] = None
    _intervals: Tuple[str, ...] = field(default=INTERVALS)

    def cycle(self, now: datetime, bars_1m: Mapping[str, pd.DataFrame],
              adv_shares: Optional[Mapping[str, float]] = None) -> List[Dict[str, Any]]:
        """Run one minute. Returns the audit records written in this cycle."""
        start = len(self.audit.records)
        adv = dict(adv_shares or {})
        self.scanner.adv_shares = adv
        state = market_session_state(now, entry_cutoff=self.entry_cutoff, calendar=self.scanner.calendar)
        if not state["open"]:
            self.audit.append("cycle_skipped", {"at": now.isoformat(), "reason": state["reason"]})
            self._save()
            return list(self.audit.records[start:])

        frames: Dict[str, Dict[str, pd.DataFrame]] = {}
        for symbol, bars in bars_1m.items():
            try:
                frames[symbol] = build_frames(bars, now, self._intervals)
            except StaleData as exc:
                self.audit.append("stale_data", {"symbol": symbol, "at": now.isoformat(), "detail": str(exc)})

        self.positions.update(frames, adv)                  # exits are audited by the manager, never blocked here
        if self.recon is not None and not self.recon.entries_allowed:
            self.audit.append("entries_blocked", {"at": now.isoformat(), "reason": f"broker recon {self.recon.status}",
                                                   "mismatches": self.recon.mismatches, "error": self.recon.error})
        else:
            self.scanner.cycle(now, frames, open_notional=self.positions.open_notional())
        self._save()
        return list(self.audit.records[start:])

    def _save(self) -> None:
        if self.state_path is not None:
            save_state(self.state_path, positions=self.positions, strategies=self.strategies,
                       handoff=self.handoff)


def build_paper_runtime(*, strategies: Mapping[str, List[MultiTimeframeStrategy]], limits: RiskLimits,
                        tiers: TierTable, calendar: Any, capital: float, risk_fraction: float,
                        skip_dates: Iterable, ledger_path: Path, kill_path: Path,
                        audit: Any = None, entry_cutoff: str = "14:45",
                        state_path: Optional[Path] = None, handoff: Any = None,
                        broker_positions: Optional[Callable[[], Dict[str, int]]] = None,
                        require_broker: bool = False) -> PaperRuntime:
    """Wire one paper runtime. The caller builds the strategies. For each symbol, TOP and BOTTOM must share
    one Handoff object (see ocl_sell_fail.Handoff); pass that same object as `handoff` so its marks are saved.

    If state_path exists, open positions, strategy state and handoff marks are restored from it, so a
    restart continues where the last cycle stopped."""
    audit = audit if audit is not None else ListAudit()
    kill = KillSwitch(kill_path)
    safe = SafeMode()
    flat = [s for lst in strategies.values() for s in lst]

    # The gate and the exit submit both reach the executor and the positions through names that are
    # bound later in this function. They are only called during a cycle, after everything is wired.
    def gate(intent):
        state = RiskState(open_notional=positions.open_notional(), open_symbols=positions.open_symbols())
        funds = capital - positions.open_notional()
        return check_intent(intent, state, limits, kill, safe, available_funds=funds)

    def submit_exit(intent):
        return executor.submit_child(intent)

    positions = PaperPositionManager(audit, submit=submit_exit)
    sender = PaperSender()
    executor = PovExecutor(gate=gate, sender=sender, api_key=PAPER_KEY, audit=audit, pov=0.1)
    scanner = Scanner(strategies=strategies, executor=executor, ledger=SignalLedger(ledger_path),
                      audit=audit, capital=capital, risk_fraction=risk_fraction, limits=limits,
                      tiers=tiers, calendar=calendar, skip_dates=list(skip_dates),
                      entry_cutoff=entry_cutoff, positions=positions)

    if state_path is not None:
        data = load_state(Path(state_path))
        if data is not None:
            restore_all(data, positions=positions, strategies=flat, handoff=handoff)
            audit.append("state_restored", {"positions": len(positions.open_positions())})

    # Broker reconcile runs AFTER the restore, so the bot's view is complete before the comparison.
    # Read-only. A mismatch or failed read blocks new entries (exits still run).
    recon = startup_reconcile(positions, broker_positions, audit=audit, require_broker=require_broker)

    return PaperRuntime(scanner=scanner, positions=positions, sender=sender, audit=audit,
                        entry_cutoff=entry_cutoff, state_path=Path(state_path) if state_path else None,
                        strategies=flat, handoff=handoff, recon=recon)


def seconds_to_next_minute(now: datetime) -> float:
    """Seconds until the next whole minute, so cycles run on the minute."""
    return 60.0 - (now.second + now.microsecond / 1e6)


def run_loop(runtime: PaperRuntime, fetch: Callable[[datetime], Tuple[Dict[str, pd.DataFrame], Dict[str, float]]],
             *, now_fn: Callable[[], datetime] = datetime.now,
             sleep_fn: Callable[[float], None] = _time.sleep,
             stop_fn: Callable[[], bool] = lambda: False) -> int:
    """Run cycles on the minute until stop_fn() is true. A failed fetch is audited and the loop goes on.
    Returns the number of cycles run."""
    cycles = 0
    while not stop_fn():
        now = now_fn()
        try:
            bars, adv = fetch(now)
        except Exception as exc:                            # fail closed for this minute; keep running
            runtime.audit.append("fetch_failed", {"at": now.isoformat(), "error": repr(exc)})
            bars, adv = {}, {}
        runtime.cycle(now, bars, adv)
        cycles += 1
        sleep_fn(seconds_to_next_minute(now_fn()))
    return cycles
