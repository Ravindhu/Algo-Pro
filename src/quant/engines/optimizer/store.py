"""
optimizer/store.py - crash-safe persistence for studies and reports.

WHY A FILE FORMAT THIS BORING
-----------------------------
JSONL, one record per line, written append-only with an explicit flush+fsync
boundary per trial. Three properties fall out of that choice, and each one
protects a real failure mode:

1. CRASH-SAFE. A killed process loses at most the line it was writing, never
   the whole study. A single JSON blob written at the end loses everything
   after trial 47 of 200 - exactly the studies worth keeping.
2. RESUMABLE BY REPLAY. Reading the file back and skipping duplicate trial
   numbers reconstructs the study exactly (last write per number wins), which
   is how a re-run continues instead of restarting.
3. DIFFABLE. Two runs of the same study differ line-by-line, which is how a
   reproducibility bug gets found - a JSON blob hides it in a re-serialised
   dict ordering.

Layout under the data root (override with BLUE_OPTIMIZER_DATA_ROOT):
    optimizer_studies/<study_name>.jsonl    the trial journal
    optimizer_reports/<study_name>.json     the final report (JSON)
    optimizer_reports/<study_name>.md       the final report (Markdown)
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from . import logger
from .study import Study, TrialRecord

__all__ = ["StudyStore", "load_study_jsonl", "study_path", "report_paths"]


def study_path(name: str) -> str:
    from . import config
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name))
    return os.path.join(config.STUDIES_DIR, f"{safe}.jsonl")


def report_paths(name: str) -> Dict[str, str]:
    from . import config
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name))
    return {"json": os.path.join(config.REPORTS_DIR, f"{safe}.json"),
            "md": os.path.join(config.REPORTS_DIR, f"{safe}.md")}


class StudyStore:
    """Append-only journal for one study."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.path = study_path(name)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def append(self, rec: TrialRecord) -> None:
        """One trial record, flushed before returning.

        The fsync is not paranoia: on Windows a process killed mid-run can lose
        OS-buffered writes, and the journal's whole job is to survive that.
        """
        line = json.dumps(rec.to_dict(), sort_keys=True, default=str)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def exists(self) -> bool:
        return os.path.isfile(self.path)


def load_study_jsonl(name: str) -> List[Dict[str, Any]]:
    """Replay a journal. Last record per trial number wins (resume semantics)."""
    path = study_path(name)
    if not os.path.isfile(path):
        return []
    by_number: Dict[int, Dict[str, Any]] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001 - a torn last line is expected
                continue
            n = rec.get("number")
            if isinstance(n, int):
                by_number[n] = rec
    return [by_number[k] for k in sorted(by_number)]


def write_report(name: str, payload: Dict[str, Any],
                 markdown: Optional[str] = None) -> Dict[str, str]:
    """Write the JSON (always) and Markdown (when given) report atomically."""
    from . import config
    os.makedirs(config.REPORTS_DIR, exist_ok=True)
    paths = report_paths(name)
    _atomic_write(paths["json"], json.dumps(payload, indent=2, sort_keys=True,
                                            default=str))
    if markdown:
        _atomic_write(paths["md"], markdown)
    return paths


def _atomic_write(path: str, text: str) -> None:
    """Write via a temp file + rename so a crash never leaves a half-report.

    A half-written report is worse than no report: it looks like a finished
    artefact. The rename is the standard trick that makes the file appear
    either fully old or fully new, never in between.
    """
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Study <-> journal helpers used by the CLI
# --------------------------------------------------------------------------- #


def study_from_journal(name: str, *, space_name: str = "",
                       strategy: str = "",
                       objective_version: str = "unknown") -> Optional[Study]:
    """Rebuild a Study object from a journal (for re-analysis without re-run)."""
    rows = load_study_jsonl(name)
    if not rows:
        return None
    study = Study(name=name, space_name=space_name, strategy=strategy,
                  objective_version=objective_version)
    for r in rows:
        study.add(TrialRecord(
            number=int(r.get("number", 0)),
            params=dict(r.get("params") or {}),
            value=r.get("value"), ok=bool(r.get("ok")),
            reason=r.get("reason"),
            mean_sharpe=r.get("mean_sharpe"),
            std_sharpe=r.get("std_sharpe"),
            min_sharpe=r.get("min_sharpe"),
            n_folds=int(r.get("n_folds") or 0),
            n_obs=int(r.get("n_obs") or 0),
            seconds=float(r.get("seconds") or 0.0),
            cached=bool(r.get("cached")),
            # AUDIT FINDING F2. The cost-gate dict is round-tripped through the
            # JSONL so a study reloaded later still has the gate on each trial.
            # Older journals (pre-F2) will not have this key - that is fine,
            # ``.get`` returns None and the report falls through to the same
            # generic message it always did for missing data.
            cost_gate=(dict(r["cost_gate"]) if isinstance(r.get("cost_gate"),
                                                       dict) else None),
            ts=float(r.get("ts") or time.time())))
    return study
