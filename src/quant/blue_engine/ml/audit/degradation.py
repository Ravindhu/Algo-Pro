"""
ml/audit/degradation.py — the visible record of "what we could not get".

WHY THIS FILE EXISTS
--------------------
The AGENTS.md rule is: **"nothing in your report is a guess."**

The most common way a quant system breaks that rule is not a wrong number — it
is a *missing* number that nobody notices. The model silently trains on fewer
rows, the walk-forward looks fine because the missing chunk happened to be
ordinary, and three weeks later someone discovers the strategy never saw a
single RBI policy day.

So every failure to obtain data becomes a first-class, persisted, queryable
record: a Degradation row. The dashboard reads these and shows a red badge with
the *exact* reason. No inference, no "probably fine".

CASE STUDY (why the `stage` field matters)
------------------------------------------
A macro dataset failed to download on 3 of 40 days. The engineer re-ran the
loader, it "worked", and the run proceeded. What actually happened: the loader
fallback used the *previous* day's value for the missing days. That forward-fill
was invisible in the training metrics but created a subtle autocorrelation the
model latched onto, and the live signal became systematically late by one day.

With a Degradation row, that run would have been marked `degraded`, the training
gate would have refused, and the operator would have seen "3 of 40 days missing
from MoSPI CPI (HTTP 503)" on the dashboard instead of a plausible-looking model.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .. import config, logger


class Severity(str, Enum):
    """How badly a degradation hurts a training run.

    The three levels map to three *different actions*, which is why they are an
    enum and not a boolean `ok` flag:

        INFO      -> log it, continue. (e.g. an optional source is off)
        DEGRADED  -> training may proceed ONLY with an explicit written reason.
        FATAL     -> training MUST NOT proceed. Data is too thin to be honest.
    """

    INFO = "info"
    DEGRADED = "degraded"
    FATAL = "fatal"


@dataclass
class Degradation:
    """One thing that went wrong while acquiring or validating data.

    Fields (all rendered verbatim on the dashboard, so keep them operator-readable):

    stage      Where it happened: 'fetch' | 'curate' | 'feature' | 'split' | 'eval'.
               Tells you who to fix — a data engineer for 'fetch', you for 'feature'.
    source     The source module name, e.g. 'nse_bhavcopy' or 'rbi_reference_rate'.
    symbol     Instrument/scope affected, or '*' for a whole-market dataset.
    severity   See Severity. Drives whether the run is allowed to continue.
    reason     Machine-readable short code, e.g. 'http_503', 'empty_window',
               'schema_drift', 'below_min_rows'. Stable so it can be graphed.
    detail     Human sentence with the numbers. This is the line the user reads.
    context    Free-form dict for reproducing it: url, params, row counts,
               date range, row hash. Never put secrets here.
    occurred_at  Unix seconds when it was recorded.
    """

    stage: str
    source: str
    reason: str
    detail: str
    severity: Severity = Severity.DEGRADED
    symbol: str = "*"
    context: Dict[str, Any] = field(default_factory=dict)
    occurred_at: float = field(default_factory=time.time)

    # ---- presentation helpers used by the dashboard ----------------------- #

    def colour(self) -> str:
        """Badge colour for the UI. Kept here so every page agrees."""
        return {
            Severity.INFO: "#22c55e",       # green — informational
            Severity.DEGRADED: "#f59e0b",   # amber — usable with caution
            Severity.FATAL: "#ef4444",      # red   — blocked
        }[self.severity]

    def line(self) -> str:
        """Single-line, greppable log form. Includes every field that matters."""
        return (f"[{self.severity.value.upper()}] {self.stage}/{self.source} "
                f"({self.symbol}) {self.reason}: {self.detail}")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value      # JSON-friendly
        return d


class DegradationLog:
    """Append-only JSONL log of degradations, plus in-memory copies per run.

    Why append-only JSONL and not a DB table:
      * it survives a crash mid-run (each line is flushed),
      * it is diffable in git if a user chooses to commit it,
      * it needs no schema migration when we add a field,
      * the dashboard can tail it without a DB connection.

    Thread-safety: a single `threading.Lock` guards the append. Training runs are
    normally single-threaded, but data *fetching* is often parallelised, and two
    processes appending without a lock can interleave a partial line.
    """

    def __init__(self, path: Optional[str] = None, *, run_id: Optional[str] = None):
        self.path = Path(path or os.path.join(config.AUDIT_DIR, "degradations.jsonl"))
        self.run_id = run_id or f"run-{int(time.time())}"
        self._rows: List[Degradation] = []
        self._lock = __import__("threading").Lock()

    # ---- recording -------------------------------------------------------- #

    def record(self, deg: Degradation) -> Degradation:
        """Store one degradation, log it, and flush it to disk. Never raises.

        The write is best-effort on purpose: failing to *record* a data problem
        must not itself become a crash that hides the data problem.
        """
        with self._lock:
            self._rows.append(deg)
            logger.warning("degradation %s", deg.line())
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"run_id": self.run_id, **deg.to_dict()},
                                        default=str) + "\n")
            except Exception as exc:  # noqa: BLE001 - logging must never break a run
                logger.error("could not persist degradation: %r", exc)
        return deg

    def info(self, stage: str, source: str, reason: str, detail: str,
             **kw: Any) -> Degradation:
        return self.record(Degradation(stage=stage, source=source, reason=reason,
                                       detail=detail, severity=Severity.INFO, **kw))

    def degraded(self, stage: str, source: str, reason: str, detail: str,
                 **kw: Any) -> Degradation:
        return self.record(Degradation(stage=stage, source=source, reason=reason,
                                       detail=detail, severity=Severity.DEGRADED,
                                       **kw))

    def fatal(self, stage: str, source: str, reason: str, detail: str,
              **kw: Any) -> Degradation:
        return self.record(Degradation(stage=stage, source=source, reason=reason,
                                       detail=detail, severity=Severity.FATAL, **kw))

    # ---- queries ---------------------------------------------------------- #

    @property
    def rows(self) -> List[Degradation]:
        return list(self._rows)

    def fatal_rows(self) -> List[Degradation]:
        return [d for d in self._rows if d.severity is Severity.FATAL]

    def degraded_rows(self) -> List[Degradation]:
        return [d for d in self._rows if d.severity is Severity.DEGRADED]

    def has_fatal(self) -> bool:
        """True when the run must stop. The training gate calls exactly this."""
        return bool(self.fatal_rows())

    def allowed_to_train(self, *, allow_degraded: bool = False,
                         reason: Optional[str] = None) -> bool:
        """Decide whether training may proceed — and *why not* if it may not.

        Rules, in order:
          1. any FATAL            -> never. No override exists, by design.
          2. any DEGRADED         -> only with an explicit reason string. An
             empty/None reason is treated as "no override" so a caller cannot
             accidentally bypass the gate with a blank argument.
          3. otherwise            -> yes.

        Returns a bool. The *explanation* for a refusal is available from
        :meth:`refusal_reason`, so callers can render one clear sentence.
        """
        if self.has_fatal():
            return False
        if self.degraded_rows():
            return bool(allow_degraded and (reason or "").strip())
        return True

    def refusal_reason(self, *, allow_degraded: bool = False,
                       reason: Optional[str] = None) -> Optional[str]:
        """One operator-facing sentence explaining refusal, or None if allowed."""
        if self.allowed_to_train(allow_degraded=allow_degraded, reason=reason):
            return None
        if self.has_fatal():
            first = self.fatal_rows()[0]
            return (f"BLOCKED: fatal data problem — {first.line()}. "
                    f"Fix the source and re-run; this run cannot be overridden.")
        n = len(self.degraded_rows())
        first = self.degraded_rows()[0]
        return (f"BLOCKED: {n} degradation(s) with no written reason. "
                f"First: {first.line()}. Re-run with allow_degraded=True and a "
                f"reason, or fix the data.")

    def summary(self) -> Dict[str, Any]:
        """Compact dict for the dashboard ML page (counts + first few details)."""
        by_sev: Dict[str, int] = {}
        for d in self._rows:
            by_sev[d.severity.value] = by_sev.get(d.severity.value, 0) + 1
        return {
            "run_id": self.run_id,
            "total": len(self._rows),
            "by_severity": by_sev,
            "fatal": [d.to_dict() for d in self.fatal_rows()][:5],
            "degraded": [d.to_dict() for d in self.degraded_rows()][:10],
            "can_train": self.allowed_to_train(),
            "blocked_because": self.refusal_reason(),
        }

    # ---- reading history back -------------------------------------------- #

    @staticmethod
    def read_all(path: Optional[str] = None,
                 *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Read previously persisted rows (newest last). Best-effort."""
        p = Path(path or os.path.join(config.AUDIT_DIR, "degradations.jsonl"))
        if not p.is_file():
            return []
        out: List[Dict[str, Any]] = []
        try:
            with p.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue          # a torn last line from a killed process
        except Exception as exc:  # noqa: BLE001
            logger.error("could not read degradation log: %r", exc)
            return []
        return out[-limit:] if limit else out


def first_fatal(degs: Iterable[Degradation]) -> Optional[Degradation]:
    """Return the first FATAL row, else None. Convenience for scripts."""
    for d in degs:
        if d.severity is Severity.FATAL:
            return d
    return None


__all__ = ["Severity", "Degradation", "DegradationLog", "first_fatal"]