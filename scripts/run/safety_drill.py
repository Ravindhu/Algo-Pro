"""Run the safety drill (Phase 3 exit check 4) and write the evidence.

Usage:
    python scripts/run/safety_drill.py                 # default output folder
    python scripts/run/safety_drill.py --out <dir>     # custom folder

Writes data/drills/<timestamp>/report.json and appends to the audit log.
Exit code 0 when every phase passed, 1 when any phase failed.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.audit.log import AuditLog  # noqa: E402
from quant.monitor.drill import run_drill  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", default=None, help="output folder (default data/drills/<ts>)")
    args = p.parse_args()

    ts = time.strftime("%Y%m%d-%H%M%S")
    out = pathlib.Path(args.out) if args.out else ROOT / "data" / "drills" / ts
    out.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(out / "audit.jsonl")

    report = run_drill(out, audit)
    path = out / "report.json"
    path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    print(json.dumps(report.to_dict(), indent=2))
    print(f"\nreport: {path}")
    print("audit : " + str(out / "audit.jsonl"))
    if report.ok:
        print("\nRESULT: PASS - kill switch and safe mode verified, no orders sent")
        return 0
    print("\nRESULT: FAIL - see the phases above")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
