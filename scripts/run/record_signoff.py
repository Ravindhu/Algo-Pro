"""Record the owner sign-off for a live start (audit fix 4, 2026-11-26).

Runs the safety drill (no orders sent) and writes data/bot_live/signoff.json with the config
fingerprint. The live start refuses unless this record is recent and matches the config on disk.

The analyze-mode soak is NOT measured here. You attest to it with --soak-sessions: the number of
clean analyze-mode trading sessions you ran before this. Minimum: 5.

Usage:
  python scripts/run/record_signoff.py --soak-sessions 5 --soak-notes "5 clean sessions, no errors" \
      --approved-by "Your Name"
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.core import signoff  # noqa: E402
from quant.monitor.drill import run_drill  # noqa: E402

CONFIG_FILES = [ROOT / "config" / "bot.yaml", ROOT / "config" / "settings.yaml",
                ROOT / "config" / "risk_limits.yaml", ROOT / "config" / "data_sources.yaml"]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state-dir", default=str(ROOT / "data" / "bot_live"))
    p.add_argument("--soak-sessions", type=int, required=True)
    p.add_argument("--soak-notes", default="")
    p.add_argument("--approved-by", required=True)
    args = p.parse_args(argv)

    state = pathlib.Path(args.state_dir)
    report = run_drill(state / "drill")
    for ph in report.phases:
        print(("PASS " if ph.passed else "FAIL ") + ph.name + ": " + ph.detail)
    try:
        record = signoff.build_signoff(
            fingerprint=signoff.config_fingerprint(CONFIG_FILES), drill_ok=report.ok,
            soak_sessions=args.soak_sessions, soak_notes=args.soak_notes,
            approved_by=args.approved_by, today=date.today())
    except ValueError as exc:
        print(json.dumps({"recorded": False, "reason": str(exc)}, indent=2))
        return 1
    signoff.save_signoff(state / "signoff.json", record)
    print(json.dumps({"recorded": True, "file": str(state / "signoff.json"), **record}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
