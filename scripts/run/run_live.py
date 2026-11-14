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

Exit checks still open (docs/IMPLEMENTATION_PLAN.md Phase 3): SEBI regulatory
verification (check 2) and the no-orders kill-switch drill (check 4). Running
this script does not claim either is satisfied.
"""
from __future__ import annotations

import argparse
import json
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


def _allowed_egress_ip() -> Optional[str]:
    """allowed_egress_ip from settings.yaml, or None when unset (check skipped)."""
    import yaml
    settings = ROOT / "config" / "settings.yaml"
    if not settings.is_file():
        return None
    try:
        data = yaml.safe_load(settings.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    value = (data.get("live") or {}).get("allowed_egress_ip")
    return str(value).strip() if value else None


def startup_checks(limits_path=pathlib.Path(LIMITS_FILE),
                   mode_file=MODE_FILE, approval_file=APPROVAL_FILE,
                   holidays_file=HOLIDAYS_FILE, audit: Optional[AuditLog] = None
                   ) -> dict:
    """All seven gates, evaluated in order. Returns {ok, reasons, approval}.

    Pure checks: no network, no orders. Written separately so tests can prove
    each gate blocks on its own before the runner ever touches a broker.
    """
    reasons: list = []

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
        approval = LiveApproval(
            approver=json.loads(approval_file.read_text(encoding="utf-8"))["approver"],
            approved_on=__import__("datetime").date.fromisoformat(
                json.loads(approval_file.read_text(encoding="utf-8"))["approved_on"]),
            limits_sha256=json.loads(approval_file.read_text(encoding="utf-8"))["limits_sha256"],
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
        # Only the REQUIRED limits block a live start. The book-level caps
        # (max_sector_pct, max_beta, max_cvar, max_drawdown) are optional: the
        # pre-gate refuses those checks until each cap is set, so an unset optional
        # cap cannot let risk through. Blocking the start on them would have made
        # live impossible until every optional cap was filled in by hand.
        unset = limits.unset_required() if hasattr(limits, "unset_required") else limits.unset()
        if unset:
            reasons.append("risk limits incomplete (null): " + ", ".join(unset))

    # 5. kill switch
    kill_path = pathlib.Path(out_dir_default()) / "kill_switch.json"
    if kill_path.exists():
        try:
            kill = json.loads(kill_path.read_text(encoding="utf-8"))
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

    # 8. egress IP (SEBI/NSE: API access needs a broker-whitelisted static IP).
    # Optional: set allowed_egress_ip in settings.yaml once the owner whitelists an
    # address. Absent = check skipped, which is correct until the owner confirms it.
    egress = _egress_ip()
    allowed = _allowed_egress_ip()
    if allowed and egress is not None and egress != allowed:
        reasons.append(f"egress IP {egress} is not the whitelisted {allowed}; "
                       "SEBI/NSE require API access from a whitelisted static IP")

    if audit is not None and reasons:
        audit.append("live_start_refused", {"reasons": reasons})

    approval_obj = approval
    return {"ok": not reasons, "reasons": reasons, "approval": approval_obj}


def out_dir_default() -> str:
    return str(ROOT / "data" / "live")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--signals", required=True)
    p.add_argument("--limits", default=str(LIMITS_FILE))
    p.add_argument("--out", default=out_dir_default())
    p.add_argument("--base-url", required=True, help="OpenAlgo base URL")
    args = p.parse_args()

    audit = AuditLog(pathlib.Path(args.out) / "audit.jsonl")
    checks = startup_checks(limits_path=pathlib.Path(args.limits), audit=audit)
    if not checks["ok"]:
        print(json.dumps({"started": False, "refused": checks["reasons"]}, indent=2))
        return 2

    api_key = OpenAlgoClient.api_key_from_env()
    client = OpenAlgoClient(args.base_url, http_transport, mode="live",
                            live_approval=checks["approval"],
                            limits_path=pathlib.Path(args.limits))
    from quant.execution.openalgo_client import AnalyzeBroker  # same adapter, live client
    broker = AnalyzeBroker(client, api_key)

    # run() is imported late so the checks above run before any trade-path import
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "run_paper", ROOT / "scripts" / "run_paper.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    summary = mod.run(args.signals, args.limits, args.out, broker=broker,
                      client=client, api_key=api_key)
    print(json.dumps({"started": True, **summary}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
