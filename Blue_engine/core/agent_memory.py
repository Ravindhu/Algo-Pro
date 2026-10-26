"""
agent_memory.py - the three memories an agentic stage skill is allowed to have.

WHY THIS FILE EXISTS
====================
A stateless skill re-learns the same lessons every cycle: it re-asks for data it
already fetched, repeats a mistake it was corrected for yesterday, and produces
the same clamped value it always produced. Three memories fix that, and each has
a different lifetime, a different owner and a different blast radius:

======================  ============  ==================  =====================
memory                  lifetime      written by          used for
======================  ============  ==================  =====================
ACTIVE WORKING          this shift    the pipeline        reuse evidence already
  (`WorkingMemory`)     (one run,     after each station  fetched; warn a station
                        or last N     runs                its input changed
                        cycles)
HISTORICAL              long-lived,   the pipeline        show the model what
  CALIBRATION           per stage     after each accepted value it (or the rails)
  (`CalibrationMemory`)               answer              usually produces
MISTAKE                 long-lived,   the pipeline        put the exact past
  (`MistakeMemory`)     per stage +   after each failure  failure in the prompt so
                        signature                         the same error is not
                                                          repeated
======================  ============  ==================  =====================

DESIGN RULES (they matter more than the features)
-------------------------------------------------
1. **JSON ONLY.** Every memory is a JSON file under the writer sandbox
   (``data/agent_stage/_memory/``). Nothing here writes code or config, and the
   paths go through :class:`~blue_engine.core.skill_writer.SkillWriter`'s
   rules - the same refusal list, the same atomic write.
2. **BOUNDED.** A memory that grows forever becomes a slow prompt and a privacy
   problem. Working memory keeps the last N stations, calibration keeps a rolling
   window of observations, and mistakes are capped by count and by distinct
   signature.
3. **ADVISORY, NEVER AUTHORITATIVE.** Memory enters the prompt as CONTEXT. It
   never sets an operative number, never clears a veto, never bypasses the
   validator. A poisonable store must not be able to trade.
4. **EVIDENCE, NOT OPINION.** Working memory stores *what was fetched* and *what
   changed*, with timestamps. Calibration stores observed values, not "good" or
   "bad". Mistake memory stores the failure signature the deterministic judge
   produced - not the model's own commentary.
5. **NEVER RAISES.** A corrupt, absent or oversized memory file degrades to
   "empty memory" with a recorded reason. A memory problem must never block a
   cycle.
6. **NO SECRETS.** Every entry passes the same credential guard the agent input
   uses (``_SECRET_TOKEN``); anything that looks like a key is dropped, not
   stored.

WHY NOT A VECTOR STORE / DB
---------------------------
Not because it is hard, but because it would be un-auditable. The whole point of
this layer is that an operator can open one JSON file and see exactly what the
agent remembered. If retrieval ever needs embeddings, it belongs behind this same
interface - the prompt builder must not care.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_CYCLES",
    "MAX_OBSERVATIONS",
    "MAX_MISTAKES",
    "MEMORY_DIR",
    "WorkingMemory",
    "CalibrationMemory",
    "MistakeMemory",
    "AgentMemory",
    "memory_context_block",
]

#: Working memory keeps the last N cycles of station hand-offs.
MAX_CYCLES = 5

#: Calibration keeps a rolling window per stage; older rows are dropped.
MAX_OBSERVATIONS = 50

#: Mistake memory is capped by count AND by distinct signature (see below).
MAX_MISTAKES = 30

#: Sub-directory inside the skill-writer sandbox. Deliberately under ``_`` so
#: the writer's audit directory and these do not collide.
MEMORY_DIR = "_memory"

#: Mirror of ``frontline_agent._SECRET_TOKEN``: a memory must never store a key.
_SECRET_TOKEN = re.compile(r"\b(?:sk-or-v1-|sk-proj-|sk-)[A-Za-z0-9_-]{20,}\b")

#: A short string is all a prompt needs. Long free text is truncated, not kept.
MAX_TEXT = 400


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _clean(value: Any, limit: int = MAX_TEXT) -> Any:
    """Make a value JSON-safe, bounded, and free of credential-shaped strings."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"),
                                                         float("-inf")) else None
    if isinstance(value, str):
        if _SECRET_TOKEN.search(value):
            return "[redacted: credential-shaped text]"
        return value[:limit]
    if isinstance(value, Mapping):
        return {str(k)[:64]: _clean(v, limit) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(v, limit) for v in list(value)[:40]]
    return str(value)[:limit]


# --------------------------------------------------------------------------- #
# 1. ACTIVE WORKING MEMORY
# --------------------------------------------------------------------------- #
@dataclass
class WorkingMemory:
    """What the line knows *right now*, for this shift and the last few.

    Scope: which station produced what, which evidence was fetched, and which
    inputs changed since the previous cycle. It exists so a station does not
    re-fetch evidence another station already brought in, and so a downstream
    station can be told "your input changed" instead of silently re-using it.

    Files: ``_memory/working.json`` (bounded to the last ``max_cycles`` cycles).
    """

    path: str = ""
    cycles: List[Dict[str, Any]] = field(default_factory=list)
    max_cycles: int = MAX_CYCLES

    # ---- lifecycle ------------------------------------------------------ #
    @classmethod
    def empty(cls, path: str = "") -> "WorkingMemory":
        return cls(path=path)

    def begin_cycle(self, cycle: str, *, product: str = "") -> Dict[str, Any]:
        """Open a cycle record and return it. Old cycles are dropped, not kept."""
        record = {"cycle": str(cycle), "started_at": _now_iso(),
                  "product": product, "stations": {}, "evidence": {}}
        self.cycles = (self.cycles + [record])[-self.max_cycles:]
        return record

    def record_station(self, stage: str, *, produced: Any = None,
                       evidence: Optional[Iterable[str]] = None,
                       state: str = "") -> None:
        """Note what one station produced and which evidence it now holds."""
        if not self.cycles:
            self.begin_cycle(_now_iso())
        current = self.cycles[-1]
        current["stations"][str(stage)] = {
            "state": state,
            "produced": _clean(produced),
            "at": _now_iso(),
        }
        for item in evidence or ():
            current["evidence"][str(item)[:120]] = _now_iso()

    def previous(self) -> Optional[Dict[str, Any]]:
        return self.cycles[-2] if len(self.cycles) >= 2 else None

    def changed_inputs(self, stage: str) -> List[str]:
        """Inputs for ``stage`` that differ from the previous cycle.

        A station that sees ``["macro"]`` here knows its upstream part moved and
        should not reuse the old conclusion blindly.
        """
        previous = self.previous()
        if not previous:
            return []
        now = (self.cycles[-1]["stations"].get(stage) or {}).get("produced")
        before = (previous["stations"].get(stage) or {}).get("produced")
        return [stage] if now != before and (now is not None or before is not None) else []

    def evidence_already_have(self) -> List[str]:
        """Everything fetched this cycle - so a station need not fetch it again."""
        if not self.cycles:
            return []
        return sorted(self.cycles[-1]["evidence"])

    def summary(self) -> Dict[str, Any]:
        """Prompt-sized view: last cycle's stations + evidence + what changed."""
        if not self.cycles:
            return {"cycles": 0}
        current = self.cycles[-1]
        return {
            "cycles": len(self.cycles),
            "cycle": current.get("cycle"),
            "stations": {k: v.get("state") for k, v in current["stations"].items()},
            "evidence_available": sorted(current["evidence"])[:20],
            "changed_this_cycle": {
                stage: self.changed_inputs(stage)
                for stage in current["stations"] if self.changed_inputs(stage)
            },
        }

    def to_dict(self) -> Dict[str, Any]:
        return {"version": 1, "kind": "working", "max_cycles": self.max_cycles,
                "cycles": self.cycles}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], path: str = "") -> "WorkingMemory":
        cycles = raw.get("cycles") if isinstance(raw, Mapping) else None
        return cls(path=path, cycles=list(cycles or [])[-MAX_CYCLES:])


# --------------------------------------------------------------------------- #
# 2. HISTORICAL CALIBRATION MEMORY
# --------------------------------------------------------------------------- #
@dataclass
class CalibrationMemory:
    """What a station *usually* produces, and how the rails usually respond.

    Scope: per stage, a rolling window of the values the deterministic engine
    produced, the values the agent proposed, and whether the safe rails clamped
    them. It exists so the model can be shown its own distribution ("your
    slippage proposals were clamped in 6 of the last 8 cycles") instead of being
    told a rule it will ignore.

    It is descriptive only. Nothing here is applied to an answer.
    """

    path: str = ""
    stages: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    max_observations: int = MAX_OBSERVATIONS

    def observe(self, stage: str, *, proposed: Mapping[str, Any],
                operative: Optional[Mapping[str, Any]] = None,
                clamped: bool = False, method: str = "") -> None:
        """Record one finished station. Numbers only, never a verdict."""
        bucket = self.stages.setdefault(str(stage), {"observations": []})
        bucket["observations"].append({
            "at": _now_iso(),
            "proposed": _clean(dict(proposed)),
            "operative": _clean(dict(operative)) if operative is not None else None,
            "clamped": bool(clamped),
            "method": str(method)[:40],
        })
        bucket["observations"] = bucket["observations"][-self.max_observations:]

    def profile(self, stage: str) -> Dict[str, Any]:
        """The calibration view for one stage, or ``{}`` when empty."""
        bucket = self.stages.get(str(stage)) or {}
        rows = bucket.get("observations") or []
        if not rows:
            return {}
        clamped = sum(1 for row in rows if row.get("clamped"))
        numeric: Dict[str, List[float]] = {}
        for row in rows:
            for key, value in (row.get("operative") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    numeric.setdefault(key, []).append(float(value))
        ranges = {k: {"min": min(v), "max": max(v), "last": v[-1]}
                  for k, v in numeric.items() if v}
        return {
            "observations": len(rows),
            "clamped": clamped,
            "clamp_ratio": round(clamped / len(rows), 3),
            "operative_ranges": ranges,
            "last_method": rows[-1].get("method"),
        }

    def summary(self) -> Dict[str, Any]:
        return {stage: self.profile(stage) for stage in self.stages
                if self.profile(stage)}

    def to_dict(self) -> Dict[str, Any]:
        return {"version": 1, "kind": "calibration",
                "max_observations": self.max_observations, "stages": self.stages}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any],
                  path: str = "") -> "CalibrationMemory":
        stages = raw.get("stages") if isinstance(raw, Mapping) else None
        clean: Dict[str, Any] = {}
        for stage, bucket in (stages or {}).items():
            rows = (bucket or {}).get("observations") if isinstance(bucket, Mapping) else None
            clean[str(stage)] = {"observations": list(rows or [])[-MAX_OBSERVATIONS:]}
        return cls(path=path, stages=clean)


# --------------------------------------------------------------------------- #
# 3. MISTAKE MEMORY
# --------------------------------------------------------------------------- #
@dataclass
class MistakeMemory:
    """The exact failure signature a station made, so it is not repeated.

    Scope: per stage, a signature (what kind of failure) with a count, the last
    detail shown to the model, and the fix that worked when one did. Fed back
    into the prompt as a short, specific warning.

    Caps: ``MAX_MISTAKES`` rows total, and at most 3 distinct signatures per
    stage (the oldest is evicted), so a stage cannot fill its prompt with the
    same mistake described 20 ways.
    """

    path: str = ""
    rows: List[Dict[str, Any]] = field(default_factory=list)
    max_rows: int = MAX_MISTAKES
    max_per_stage: int = 3

    # ---- record ---------------------------------------------------------- #
    def record(self, stage: str, *, kind: str, detail: Any,
               fix: str = "", resolved: bool = False) -> None:
        """Add or refresh one mistake. Repeating a signature bumps its count.

        Every string passes through ``_clean`` so a credential-shaped value can
        never be persisted - the same guard the agent input uses. (AUDIT
        2026-10-26: the first version truncated with ``str(d)[:120]`` and stored
        a key verbatim; caught by
        ``test_a_credential_shaped_string_is_redacted_not_stored``.)
        """
        stage = str(_clean(stage, 64))
        kind = str(_clean(kind, 40))
        items = detail if isinstance(detail, (list, tuple)) else [detail]
        detail_text = "; ".join(str(_clean(str(d), 120)) for d in items)[:MAX_TEXT]
        for row in self.rows:
            if row["stage"] == stage and row["kind"] == kind:
                row["count"] += 1
                row["detail"] = detail_text or row["detail"]
                row["at"] = _now_iso()
                if fix:
                    row["fix"] = str(_clean(fix, 160))
                if resolved:
                    row["resolved"] = True
                return
        self.rows.append({
            "stage": stage, "kind": kind, "detail": detail_text,
            "fix": str(_clean(fix, 160)), "resolved": bool(resolved),
            "count": 1, "at": _now_iso(),
        })
        self._enforce_caps(stage)

    def _enforce_caps(self, stage: str) -> None:
        per_stage = [r for r in self.rows if r["stage"] == stage]
        if len(per_stage) > self.max_per_stage:
            # Evict the OLDEST, most-resolved first: a resolved mistake is the
            # least useful one to keep repeating to the model.
            victim = sorted(per_stage,
                            key=lambda r: (not r.get("resolved"), r["at"]))[0]
            self.rows.remove(victim)
        self.rows = self.rows[-self.max_rows:]

    # ---- read ------------------------------------------------------------ #
    def for_stage(self, stage: str, *, limit: int = 3) -> List[Dict[str, Any]]:
        """The most repeated unresolved mistakes for one stage."""
        rows = [r for r in self.rows if r["stage"] == str(stage)]
        rows.sort(key=lambda r: (bool(r.get("resolved")), -int(r["count"])))
        return rows[:limit]

    def summary(self) -> Dict[str, Any]:
        return {
            stage: [{"kind": r["kind"], "count": r["count"],
                     "resolved": r["resolved"]}
                    for r in self.for_stage(stage, limit=self.max_per_stage)]
            for stage in sorted({r["stage"] for r in self.rows})
        }

    def to_dict(self) -> Dict[str, Any]:
        return {"version": 1, "kind": "mistakes", "rows": self.rows}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], path: str = "") -> "MistakeMemory":
        rows = raw.get("rows") if isinstance(raw, Mapping) else None
        return cls(path=path, rows=list(rows or [])[-MAX_MISTAKES:])


# --------------------------------------------------------------------------- #
# The bundle: load / save / prompt block
# --------------------------------------------------------------------------- #
@dataclass
class AgentMemory:
    """All three memories for one process, loaded from and saved to JSON.

    ``sandbox`` is the SAME directory the skill writer uses, so an agent has
    exactly one place it may write and one place its memory lives.
    """

    sandbox: Path
    working: WorkingMemory = field(default_factory=WorkingMemory.empty)
    calibration: CalibrationMemory = field(default_factory=CalibrationMemory)
    mistakes: MistakeMemory = field(default_factory=MistakeMemory)
    load_notes: List[str] = field(default_factory=list)
    dirty: Dict[str, bool] = field(default_factory=dict)

    # ---- paths ----------------------------------------------------------- #
    @property
    def root(self) -> Path:
        return Path(self.sandbox) / MEMORY_DIR

    def _file(self, kind: str) -> Path:
        return self.root / f"{kind}.json"

    # ---- load ------------------------------------------------------------ #
    @classmethod
    def load(cls, sandbox: Any) -> "AgentMemory":
        """Load all three. A missing/corrupt file is an EMPTY memory + a note."""
        root = Path(sandbox)
        memory = cls(sandbox=root)
        memory.working.path = str(memory._file("working"))
        memory.calibration.path = str(memory._file("calibration"))
        memory.mistakes.path = str(memory._file("mistakes"))

        loaders = (
            ("working", WorkingMemory.from_dict),
            ("calibration", CalibrationMemory.from_dict),
            ("mistakes", MistakeMemory.from_dict),
        )
        for kind, loader in loaders:
            path = memory._file(kind)
            if not path.is_file():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, Mapping):
                    raise ValueError("not a JSON object")
                setattr(memory, kind, loader(raw, path=str(path)))
            except Exception as exc:  # noqa: BLE001 - memory must never block
                memory.load_notes.append(f"{kind}: {type(exc).__name__} ignored")
                logger.warning("agent_memory: %s unreadable: %r", kind, exc)
        return memory

    # ---- save ------------------------------------------------------------ #
    def save(self, *, only: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        """Atomically write the dirty memories. Never raises."""
        from .skill_writer import SkillWriter

        writer = SkillWriter(self.sandbox, audit=False)
        wanted = set(only) if only else {"working", "calibration", "mistakes"}
        result: Dict[str, Any] = {}
        for kind in sorted(wanted):
            obj = getattr(self, kind, None)
            if obj is None or not self.dirty.get(kind):
                continue
            out = writer.write_json(f"{MEMORY_DIR}/{kind}.json", obj.to_dict())
            result[kind] = out
            if out.get("ok"):
                self.dirty[kind] = False
        return result

    def mark(self, *kinds: str) -> None:
        for kind in kinds:
            self.dirty[kind] = True

    # ---- prompt ---------------------------------------------------------- #
    def prompt_block(self, stage: str, *, working: bool = True,
                     calibration: bool = True, mistakes: bool = True) -> str:
        """The memory context handed to ONE station's prompt.

        Short by construction: a station gets what changed, what it usually
        produces, and at most three past mistakes. Anything longer is noise the
        model will not read.
        """
        return memory_context_block(
            stage,
            working=self.working if working else None,
            calibration=self.calibration if calibration else None,
            mistakes=self.mistakes if mistakes else None)

    def describe(self) -> Dict[str, Any]:
        return {
            "root": str(self.root),
            "working_cycles": len(self.working.cycles),
            "calibration_stages": sorted(self.calibration.stages),
            "mistake_rows": len(self.mistakes.rows),
            "load_notes": list(self.load_notes),
            "writable": "json_only",
        }

    def summary(self) -> Dict[str, Any]:
        """JSON-safe view for the dashboard / report."""
        return {
            "working": self.working.summary(),
            "calibration": self.calibration.summary(),
            "mistakes": self.mistakes.summary(),
            "load_notes": list(self.load_notes),
        }


def memory_context_block(stage: str, working: Optional[WorkingMemory] = None,
                         calibration: Optional[CalibrationMemory] = None,
                         mistakes: Optional[MistakeMemory] = None) -> str:
    """Render the three memories as a short, plain-text prompt block."""
    lines: List[str] = []

    if working is not None:
        evidence = working.evidence_already_have()
        changed = working.changed_inputs(stage)
        if evidence:
            lines.append("EVIDENCE ALREADY IN HAND (do not ask for it again): "
                         + ", ".join(evidence[:12]))
        if changed:
            lines.append("YOUR INPUT CHANGED SINCE THE LAST CYCLE: "
                         + ", ".join(changed))

    if calibration is not None:
        profile = calibration.profile(stage)
        if profile:
            ranges = profile.get("operative_ranges") or {}
            if ranges:
                parts = [f"{k} in [{v['min']}, {v['max']}] (last {v['last']})"
                         for k, v in list(ranges.items())[:4]]
                lines.append("WHAT THIS STATION NORMALLY PRODUCES: "
                             + "; ".join(parts))
            ratio = profile.get("clamp_ratio") or 0.0
            if ratio >= 0.5:
                lines.append(
                    f"NOTE: your proposals here were tightened by the safety "
                    f"rails in {int(round(ratio * 100))}% of the last "
                    f"{profile['observations']} cycles. Propose within the "
                    f"range you can actually keep.")

    if mistakes is not None:
        rows = mistakes.for_stage(stage)
        if rows:
            lines.append("MISTAKES TO NOT REPEAT:")
            for row in rows:
                fix = f" -> {row['fix']}" if row.get("fix") else ""
                lines.append(f"- [{row['kind']} x{row['count']}] "
                             f"{row['detail']}{fix}")

    if not lines:
        return ""
    return ("MEMORY (context only - it never decides anything and never "
            "overrides the schema):\n" + "\n".join(lines))
