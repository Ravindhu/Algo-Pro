"""Live runner: the paper pipeline with a live broker and the full approval gate.

Flow stage: Live execution
Status: PHASE 4 IMPLEMENTED (decision 0030: armed but disarmed)

WHY THIS IS SMALL
-----------------
The order path is NOT re-implemented here. ``run_live`` calls
``scripts/run_paper.run()`` - the same signals CSV, the same risk gate, the same
queue, the same FSM, the same ledger and the same audit log - with a live
broker. The only new things are the guards that decide whether a live run may
start at all. Reusing the gate is the point: a live order must pass the identical
checks a paper order passes, plus the ones below.

THE GATE (decision 0030), in order. ALL of these must pass to start:
  1. mode.json says "live"            - the operator switched the dashboard over
  2. a valid LiveApproval exists      - a person signed off, via
                                        scripts/ops/approve_live.py
  3. the approval's limits hash still matches risk_limits.yaml
                                        - edited limits void the approval
  4. risk_limits.yaml has no null and is not blocked
  5. the kill switch is not active
  6. the trading calendar is verified - refuses on an unverified holiday file
  7. OPENALGO_API_KEY is present

Every failure is audited with its reason, and the run does not start. There is
no flag that skips a check: a bypass would make the gate decorative.

WHAT "LIVE" MEANS HERE
----------------------
Real orders reach OpenAlgo in ``live`` mode. The client still refuses any order
without an approved GateDecision whose intent_id matches (decision 0017), and
refuses to run at all if risk_limits.yaml changes mid-session. Nothing here
relaxes that.

Exit checks still open (IMPLEMENTATION_PLAN.md (deleted 2026-11-20, in git history) Phase 3): the no-orders
kill-switch drill (check 4). Running
this script does not claim either is satisfied.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from typing import Optional

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.audit.log import AuditLog  # noqa: E402
from quant.core.calendar import TradingCalendar  # noqa: E402
from quant.core.config import RiskLimits  # noqa: E402
from quant.core.live_approval import check_approval  # noqa: E402
from quant.core.trading_mode import trading_mode_status  # noqa: E402
from quant.execution.openalgo_client import (  # noqa: E402
    LiveApproval, OpenAlgoClient, http_transport,
)

MODE_FILE = ROOT / "data" / "mode.json"
APPROVAL_FILE = ROOT / "data" / "live_approval.json"
LIMITS_FILE = ROOT / "config" / "risk_limits.yaml"
HOLIDAYS_FILE = ROOT / "config" / "calendar" / "holidays.csv"
SETTINGS_FILE = ROOT / "config" / "settings.yaml"
#: The kill switch file the live bot uses (run_strategy_bot_live: state dir + kill.json).
LIVE_BOT_KILL_FILE = ROOT / "data" / "bot_live" / "kill.json"


def _egress_ip() -> Optional[str]:
    """The public IP this machine leaves through, or None when it cannot be found.

    Uses only the standard library. Any failure returns None and the check is
    skipped: an unreachable lookup must not block a start, and the IP question is
    really the broker's whitelisting.
    """
    import json as _json
    import urllib.request
    try:
        with urllib.request.urlopen("https://api.ipify.org?format=json", timeout=5) as r:
            return str(_json.loads(r.read().decode("utf-8")).get("ip")) or None
    except Exception:  # noqa: BLE001 - offline or blocked: skip, do not guess
        return None


def _live_settings(settings_path=SETTINGS_FILE) -> dict:
    """The ``live`` section of settings.yaml, or {} when the file is missing or unreadable."""
    import yaml
    path = pathlib.Path(settings_path)
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    live = data.get("live") if isinstance(data, dict) else None
    return live if isinstance(live, dict) else {}


def startup_checks(limits_path=pathlib.Path(LIMITS_FILE),
                   mode_file=MODE_FILE, approval_file=APPROVAL_FILE,
                   holidays_file=HOLIDAYS_FILE, audit: Optional[AuditLog] = None,
                   kill_path=None, settings_path=SETTINGS_FILE, egress_fn=None) -> dict:
    """Every start gate, evaluated in order. Returns {ok, reasons, approval, capital_cap}.

    Pure checks: no orders. The only network call is the egress IP lookup, which can be
    injected (egress_fn). kill_path must be the file the live bot's KillSwitch uses.
    A missing or null setting refuses the start (fail closed).
    """
    reasons: list = []
    kill_file = pathlib.Path(kill_path) if kill_path is not None else LIVE_BOT_KILL_FILE

    # 1. the operator switched the mode to live
    status = trading_mode_status(state_path=mode_file, approval_path=approval_file,
                                 limits_path=limits_path)
    if status["mode"] != "live":
        reasons.append(f"dashboard mode is {status['mode']!r}, not live; switch it first")
    elif status["effective_mode"] != "live":
        reasons.append(f"live mode is not armed: {status.get('reason')}")

    # 2+3. a person's approval, still matching today's limits file
    ok, why = check_approval(approval_file, limits_path)
    if not ok:
        reasons.append(f"live approval: {why}")
        approval = None
    else:
        approval_data = json.loads(approval_file.read_text(encoding="utf-8"))
        approval = LiveApproval(
            approver=approval_data["approver"],
            approved_on=__import__("datetime").date.fromisoformat(approval_data["approved_on"]),
            limits_sha256=approval_data["limits_sha256"],
        )

    # 4. the limits themselves must be complete and not blocked
    try:
        limits = RiskLimits.from_yaml(limits_path)
    except Exception as exc:  # noqa: BLE001 - a broken limits file must not start a live run
        reasons.append(f"risk limits unreadable: {exc}")
        limits = None
    if limits is not None:
        if limits.blocked:
            reasons.append("risk limits say blocked: true; live entries are stopped")
        unset = limits.unset_required()
        if unset:
            reasons.append("risk limits incomplete (null): " + ", ".join(unset))
        # A null book-level cap switches its check OFF in the pre-gate. Live must not run
        # with a drawdown, CVaR, beta or sector check that is silently off (2026-11-15).
        book = limits.unset_book_for_live()
        if book:
            reasons.append("live needs book-level caps set (null): " + ", ".join(book)
                           + "; set them in config/risk_limits.yaml")

    # 5. kill switch: the same file the live bot writes. Unreadable = not assumed clear.
    if kill_file.exists():
        try:
            kill = json.loads(kill_file.read_text(encoding="utf-8"))
            if kill.get("active"):
                reasons.append(f"kill switch is active: {kill.get('reason')}")
        except (OSError, ValueError):
            reasons.append("kill switch file unreadable: refusing to assume it is clear")

    # 6. calendar: refuse on an unverified holiday file
    cal = TradingCalendar.from_csv(holidays_file)
    if not cal.verified:
        reasons.append("holiday calendar is not verified; live refuses to run")

    # 7. the API key must exist before anything tries to send
    try:
        OpenAlgoClient.api_key_from_env()
    except RuntimeError as exc:
        reasons.append(f"OpenAlgo API key: {exc}")

    # 8. egress IP: the broker whitelists a static IP for API orders. No setting, or an
    # IP that cannot be checked, refuses the start (fail closed; was optional before 2026-11-15).
    live = _live_settings(settings_path)
    allowed = str(live.get("allowed_egress_ip") or "").strip()
    if not allowed:
        reasons.append("live needs live.allowed_egress_ip in config/settings.yaml: "
                       "the static IP the broker whitelisted for API orders")
    else:
        egress = (egress_fn or _egress_ip)()
        if egress is None:
            reasons.append("egress IP could not be checked; live refuses to start without it")
        elif egress != allowed:
            reasons.append(f"egress IP {egress} is not the whitelisted {allowed}; "
                           "the broker requires API orders from a whitelisted static IP")

    # 9. fixed capital cap: the live bot sizes against at most this much (no cap = no start)
    cap = live.get("max_capital_inr")
    capital_cap = None
    if isinstance(cap, (int, float)) and not isinstance(cap, bool) and math.isfinite(cap) and cap > 0:
        capital_cap = float(cap)
    else:
        reasons.append("live needs live.max_capital_inr in config/settings.yaml: "
                       "a fixed INR cap on the capital the live bot may use")

    if audit is not None and reasons:
        audit.append("live_start_refused", {"reasons": reasons})

    return {"ok": not reasons, "reasons": reasons, "approval": approval, "capital_cap": capital_cap}


def out_dir_default() -> str:
    return str(ROOT / "data" / "live")


def main() -> int:
    """Retired entry point (2026-11-15 audit). The old pipeline path is never used for live.

    The only live entry point is scripts/run/run_strategy_bot_live.py. That runner
    calls startup_checks() from this file, so this module stays importable. This
    function refuses to run and sends no order.
    """
    print(json.dumps({"started": False, "refused": [
        "run_live.py is retired: use scripts/run/run_strategy_bot_live.py (the only live path)"]},
        indent=2))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
