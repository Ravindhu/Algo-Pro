"""Live checklist (decision 0031): eight checks from docs/LIVE_RUN_DESIGN.md section 4.

Pure logic. The caller gathers the facts (see core/live_facts.py); this module only decides.
A fact that is None means "not verified" and FAILS its check (fail closed).
No order is placed here.

Flow stage: Dashboard live switch
Status: PHASE 3 IMPLEMENTED (checks only)
"""
from dataclasses import dataclass
from datetime import datetime, time
from typing import List, Optional, Sequence

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class LiveFacts:
    calendar_verified: bool
    is_trading_day: bool
    now_ist: datetime
    data_source_licensed: Optional[bool]        # True only when a licensed live source is approved
    approver: Optional[str]                     # typed by the person on the confirm step
    approval_limits_sha256: Optional[str]       # limits hash at approval time
    current_limits_sha256: Optional[str]        # limits hash now
    max_symbols_per_portfolio: Optional[int]
    openalgo_mode: Optional[str]                # "live", "analyze", or None (not checked)
    kill_active: Optional[bool]
    safe_mode_halt: Optional[bool]
    daily_loss_breach: Optional[bool]
    recon_clean: Optional[bool]


def evaluate(f: LiveFacts) -> List[Check]:
    t = f.now_ist.time()
    in_session = SESSION_OPEN <= t < SESSION_CLOSE
    sha_ok = bool(f.approval_limits_sha256) and f.approval_limits_sha256 == f.current_limits_sha256
    return [
        Check("calendar", bool(f.calendar_verified and f.is_trading_day),
              "calendar verified and a trading day" if f.calendar_verified and f.is_trading_day
              else "not a verified NSE equity trading day"),
        Check("session", bool(in_session and f.is_trading_day),
              f"inside 09:15-15:30 IST" if in_session else f"outside 09:15-15:30 IST ({t:%H:%M})"),
        Check("licensed_data", f.data_source_licensed is True,
              "licensed live data source approved" if f.data_source_licensed is True
              else "no licensed live data source (config/data_sources.yaml)"),
        Check("approval", bool(f.approver and str(f.approver).strip()) and sha_ok,
              "approver named and limits hash matches" if sha_ok and f.approver
              else "approver missing or limits hash changed"),
        Check("max_symbols_set", f.max_symbols_per_portfolio is not None,
              "max_symbols_per_portfolio set" if f.max_symbols_per_portfolio is not None
              else "max_symbols_per_portfolio is unset"),
        Check("openalgo", f.openalgo_mode == "live",
              "OpenAlgo in live mode" if f.openalgo_mode == "live"
              else f"OpenAlgo mode is {f.openalgo_mode or 'not checked'}"),
        Check("kill_safe_loss", f.kill_active is False and f.safe_mode_halt is False
              and f.daily_loss_breach is False,
              "kill switch off, safe mode clear, no loss breach"
              if (f.kill_active is False and f.safe_mode_halt is False and f.daily_loss_breach is False)
              else "kill switch, safe mode or loss breach active, or not checked"),
        Check("reconciliation", f.recon_clean is True,
              "reconciliation clean" if f.recon_clean is True else "reconciliation not clean or not checked"),
    ]


def allowed(checks: Sequence[Check]) -> bool:
    return len(checks) == 8 and all(c.passed for c in checks)
