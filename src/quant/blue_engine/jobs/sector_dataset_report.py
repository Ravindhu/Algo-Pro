"""
sector_dataset_report.py - build and summarise the sector cross-section dataset.

WHY A COMMAND
-------------
The dataset is research infrastructure: build it, look at its shape, confirm the
leak checks, then feed it to ``ml/evaluation/walk_forward.py``. Keeping the build
behind a command means the same code path produces the training set and the
report, so a number quoted in a review is reproducible.

USAGE
-----
    python -m blue_engine.jobs.sector_dataset_report
    python -m blue_engine.jobs.sector_dataset_report --no-persist
    python -m blue_engine.jobs.sector_dataset_report --json

EXIT CODES
----------
    0  dataset built
    1  nothing usable was built (a data problem)
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from ..sectors.sector_dataset import build_dataset


def _summarise(doc):
    prov = doc.get("provenance") or {}
    print(f"SECTOR CROSS-SECTION DATASET  ({doc.get('version')})")
    print(f"sectors      : {prov.get('n_sectors')}")
    print(f"dates        : {prov.get('n_dates')} "
          f"({', '.join(doc.get('date_range') or [])})")
    print(f"rows         : {prov.get('n_rows')}")
    print(f"median width : {prov.get('median_width')} sectors/date "
          f"(min {prov.get('min_width')}); "
          f"{prov.get('dates_dropped_narrow')} dates dropped as too narrow")
    print(f"horizons     : {doc.get('horizons')}")
    print(f"labelled     : {prov.get('labelled_rows')}")
    print(f"persisted to : {doc.get('persisted_to')}")
    print(f"\nlabel rule   : {doc.get('label_rule')}")
    print(f"features     : {', '.join(doc.get('features') or [])}")

    print("\nfirst date per sector (survivorship is visible here, not assumed):")
    for stem, first in (prov.get("first_date_per_sector") or {}).items():
        print(f"  {stem:<28} {first}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the sector cross-sectional dataset.")
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    doc = build_dataset(persist=not args.no_persist)
    if not doc.get("ok"):
        print(f"[FAIL] {doc.get('reason')}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(doc, indent=2))
    else:
        _summarise(doc)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
