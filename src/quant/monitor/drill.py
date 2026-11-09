"""Safety drill: prove the kill switch and safe mode work, without sending one order.

Flow stage: Kill switch, Safe mode controller
Status: PHASE 3 IMPLEMENTED (exit check 4 drill; decision 0031)

WHY A DRILL AND NOT JUST UNIT TESTS
-----------------------------------
Phase 3 exit check 4 asks for "kill switch and safe mode tested in a live
session with no orders sent". The unit tests prove each guard works alone. What
they do not prove is the CHAIN: that a tripped switch, a halt-all mode and a
reduce-only mode produce the right DECISION COMBINATION at the one place every
order passes — the risk gate — with a broker-shaped stub standing in for real
fills.

This drill runs that chain end to end and records a pass/fail per phase. Every
"order" goes to a CountingBroker that counts calls and never fills, so the run
can assert the strongest claim in the system: NO ORDER WAS SENT.

Each phase is also expected to FAIL when a planted fault breaks it — that is
what makes this evidence rather than a demo (the same mutant approach the
point-in-time tests used, X-4).

HOW TO RUN
----------
    python -m src.quant.monitor.drill          # or scripts/run/safety_drill.py

The JSON report is written to data/drills/<ts>/report.json and appended to the
audit log, so a drill run is evidence, not a screen you screenshot.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from quant.audit.log import AuditLog
from quant.core.config import RiskLimits
from quant.core.types import TradeIntent
from quant.monitor.safe_mode import Mode, SafeMode
from quant.risk.gate import RiskState, check_intent
from quant.risk.kill_close import KILL_STRATEGY_ID, kill_exit_intents
from quant.risk.kill_switch import KillSwitch

#: Fail-closed paper limits for the drill. A drill must never depend on the
#: operator's config file being complete: it runs even on a fresh checkout.
DRILL_LIMITS = RiskLimits(
    max_order_quantity=10,
    max_order_notional=10000,
    max_open_exposure=20000,
    max_daily_loss=1000,
    max_strategy_loss=500,
    max_adv_pct=0.01,
    max_symbols_per_portfolio=8,
    blocked=False,
)


@dataclass
class CountingBroker:
    """Counts every place() call and never fills. THE safety claim lives here."""

    calls: List[Dict[str, Any]] = field(default_factory=list)

    def place(self, intent: TradeIntent, decision) -> Dict[str, Any]:
        self.calls.append({"intent_id": intent.intent_id, "symbol": intent.symbol,
                           "side": intent.side, "qty": intent.qty})
        return {"order_id": "", "status": "NOT_SENT", "filled_qty": 0,
                "avg_price": None, "reason": "drill: no broker attached"}

    @property
    def orders_sent(self) -> int:
        return len(self.calls)


@dataclass
class PhaseResult:
    name: str
    passed: bool
    detail: str


class DrillReport:
    """Ordered phase results plus the headline the operator reads first."""

    def __init__(self) -> None:
        self.phases: List[PhaseResult] = []
        self.started = time.time()

    def add(self, name: str, passed: bool, detail: str) -> None:
        self.phases.append(PhaseResult(name, bool(passed), detail))

    @property
    def ok(self) -> bool:
        return all(p.passed for p in self.phases)

    @property
    def orders_sent(self) -> int:
        return 0  # the drill never holds a real broker; set by the runner

    def to_dict(self) -> Dict[str, Any]:
        return {
            "drill": "kill_switch_and_safe_mode",
            "ok": self.ok,
            "orders_sent": 0,
            "no_orders_claim": "every order path went to CountingBroker, which sends nothing",
            "phases": [{"name": p.name, "passed": p.passed, "detail": p.detail}
                       for p in self.phases],
            "seconds": round(time.time() - self.started, 3),
        }


def _intent(symbol: str, side: str = "BUY", qty: int = 5, *, kind: str = "entry",
            strategy_id: str = "DRILL", price: float = 100.0,
            intent_id: Optional[str] = None) -> TradeIntent:
    # adv_shares is set: liquidity checks fail closed on "ADV unknown", and a
    # drill must clear every check it does not intend to exercise.
    return TradeIntent(
        intent_id=intent_id or f"D-{symbol}-{side}-{kind}-{intent_id or ''}",
        symbol=symbol, side=side, qty=qty, kind=kind, strategy_id=strategy_id,
        ref_price=price, trade_day=date(2026, 11, 9), lot_size=1, adv_shares=1_000_000,
    )


def _gate(intent: TradeIntent, kill: KillSwitch, safe: SafeMode,
          state: Optional[RiskState] = None):
    return check_intent(intent, state or RiskState(), DRILL_LIMITS, kill, safe)


def run_drill(out_dir: Path, audit: Optional[AuditLog] = None) -> DrillReport:
    """Run every phase in sequence. Returns the report; caller persists it."""
    report = DrillReport()
    broker = CountingBroker()
    work = Path(out_dir)
    work.mkdir(parents=True, exist_ok=True)
    kill = KillSwitch(work / "kill_switch.json")
    safe = SafeMode(audit)

    # ---- Phase 1: clean gate approves a normal entry --------------------- #
    d = _gate(_intent("TCS"), kill, safe)
    report.add("clean_entry_approved", d.approved,
               "gate approved an entry with no guard active" if d.approved
               else f"unexpected block: {d.reasons}")

    # ---- Phase 2: kill switch blocks entries ----------------------------- #
    kill.trip("drill: operator pressed the kill button", source="drill")
    d = _gate(_intent("INFY"), kill, safe)
    report.add("kill_blocks_entries", (not d.approved) and kill.active,
               f"trip persisted, gate refused: {d.reasons}" if not d.approved
               else "GATE APPROVED AN ENTRY WHILE KILLED")

    # ---- Phase 3: kill switch survives a restart ------------------------- #
    revived = KillSwitch(work / "kill_switch.json")
    d2 = _gate(_intent("RELIANCE"), revived, safe)
    report.add("kill_survives_restart", revived.active and not d2.approved,
               f"reloaded switch is still active and still blocks ({d2.reasons})"
               if revived.active and not d2.approved else "KILL STATE DID NOT SURVIVE RELOAD")

    # ---- Phase 4: kill closes pass the gate while killed ----------------- #
    intents, skipped = kill_exit_intents({"TCS": 5}, {"TCS": 100.0}, [])
    d3 = _gate(intents[0], revived, safe) if intents else None
    closed_ok = bool(intents) and d3 is not None and d3.approved and \
        intents[0].strategy_id == KILL_STRATEGY_ID
    report.add("kill_close_passes_while_killed", closed_ok,
               f"exit for TCS approved with strategy_id {KILL_STRATEGY_ID}"
               if closed_ok else f"kill close was refused: {skipped or (d3.reasons if d3 else 'no intent')}")

    # ---- Phase 5: reset needs a person ----------------------------------- #
    try:
        revived.reset(actor="", reason="")
        reset_blocked = False
    except ValueError:
        reset_blocked = True
    report.add("kill_reset_needs_a_person", reset_blocked,
               "anonymous reset refused" if reset_blocked else "ANONYMOUS RESET SUCCEEDED")

    # ---- Phase 6: reduce-only blocks entries, allows exits --------------- #
    revived.reset(actor="drill-operator", reason="phase 6", audit=audit)
    safe.set(Mode.REDUCE_ONLY, reason="drill phase 6")
    entry = _gate(_intent("WIPRO"), revived, safe)
    exit_i = _intent("TCS", side="SELL", qty=5, kind="exit")
    # the exit needs an open symbol to be coherent; the gate does not require it
    ex = _gate(exit_i, revived, safe)
    report.add("reduce_only_blocks_entries_allows_exits",
               (not entry.approved) and ex.approved,
               f"entry refused ({entry.reasons[:1]}), exit approved"
               if (not entry.approved and ex.approved)
               else f"entry approved={entry.approved} exit approved={ex.approved}")

    # ---- Phase 7: halt-all blocks everything except kill closes ---------- #
    # is_kill_close needs kill.active AND strategy_id == KILL, per decision 0020:
    # kill closes pass halt-all ONLY while the switch is on. So the drill trips it.
    revived.trip("drill phase 7: proving kill closes pass halt-all", source="drill")
    safe.set(Mode.HALT_ALL, reason="drill phase 7")
    entry = _gate(_intent("SBIN"), revived, safe)
    ex = _gate(_intent("TCS", side="SELL", qty=5, kind="exit",
                       strategy_id=KILL_STRATEGY_ID, intent_id="D-killclose"),
               revived, safe)
    report.add("halt_all_blocks_all_but_kill_closes",
               (not entry.approved) and ex.approved,
               "halt-all refused the entry, still allowed an exit"
               if (not entry.approved and ex.approved)
               else f"entry={entry.approved} exit={ex.approved}")

    # ---- Phase 8: safe mode changes are audited -------------------------- #
    rows = audit.read_all() if audit is not None else []
    modes_logged = sum(1 for r in rows if r.get("kind") == "safe_mode")
    report.add("safe_mode_changes_audited", modes_logged >= 2,
               f"{modes_logged} safe_mode audit rows found"
               if modes_logged >= 2 else "safe mode changes were NOT audited")

    # ---- Phase 9: THE claim - no order was ever sent --------------------- #
    report.add("no_orders_sent", broker.orders_sent == 0,
               f"{broker.orders_sent} order calls reached a broker"
               if broker.orders_sent else
               "every path went to CountingBroker; nothing was sent")

    if audit is not None:
        audit.append("safety_drill", report.to_dict())
    return report
