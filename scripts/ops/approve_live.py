"""Approve live trading. Interactive, terminal-only. The dashboard cannot do this.

Flow stage: Risk gate
Status: PHASE 4 IMPLEMENTED (decision 0030)

WHAT THIS DOES
--------------
Reads config/risk_limits.yaml, prints it, and asks the person at the keyboard to
type an exact approval phrase. Only then writes data/live_approval.json with the
approver's name, today's date, the reason, and the SHA-256 of the limits file
they just read.

WHY IT IS NOT A DASHBOARD ROUTE
-------------------------------
decision 0030 keeps approval creation out of any API surface on purpose: a
dashboard button is one mis-click away from live money, while typing a full
sentence is not. The approval file is the ONLY thing that arms live mode, and
the only way to produce one is this script.

A changed limits file voids the approval automatically (the hash stops
matching). Re-running this script re-approves the new file. There is no way to
extend an approval without re-reading the limits.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.core.live_approval import (  # noqa: E402
    APPROVAL_PHRASE, create_approval, file_sha256, revoke_approval,
)

LIMITS = ROOT / "config" / "risk_limits.yaml"
APPROVAL = ROOT / "data" / "live_approval.json"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--limits", default=str(LIMITS))
    p.add_argument("--out", default=str(APPROVAL))
    p.add_argument("--approver", required=True, help="full name of the person approving")
    p.add_argument("--reason", required=True, help="why live trading is being approved")
    p.add_argument("--revoke", action="store_true", help="delete the approval instead")
    args = p.parse_args()

    limits = pathlib.Path(args.limits)
    if args.revoke:
        removed = revoke_approval(pathlib.Path(args.out))
        print("approval removed" if removed else "no approval file present")
        return 0

    if not limits.is_file():
        print(f"[FAIL] limits file not found: {limits}")
        return 1

    print("\n=== You are approving LIVE trading ===\n")
    print(f"Limits file: {limits}")
    print(f"SHA-256    : {file_sha256(limits)}\n")
    print(limits.read_text(encoding="utf-8"))
    print("These are the values that will cap every live order.")
    print(f"Type exactly this phrase to approve: {APPROVAL_PHRASE}\n")
    typed = input("> ")
    try:
        record = create_approval(pathlib.Path(args.out), approver=args.approver,
                                 reason=args.reason, limits_path=limits, phrase=typed)
    except ValueError as exc:
        print(f"\n[REFUSED] {exc}")
        print("Nothing was written.")
        return 1

    print(f"\n[OK] approval written to {args.out}")
    print(json.dumps(record.to_dict(), indent=2))
    print("\nNext: switch the dashboard to LIVE. The mode change re-checks this approval,")
    print("and run_live.py re-checks it again before sending anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
