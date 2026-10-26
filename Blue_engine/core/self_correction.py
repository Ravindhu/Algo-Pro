"""
self_correction.py - the automatic self-correction loop for agentic stage skills.

WHY THIS FILE EXISTS
====================
``frontline_agent.advise()`` is one shot: the model answers, the answer is
validated, and a bad answer is thrown away. The operator asked for the loop that
closes that gap automatically:

    "include automatic self correction loop for agentic ai"

THE LOOP
--------
::

    attempt 1 .. N
        |
        +--> call the skill (with read-only data tools when enabled)
        |
        +--> deterministic gate: _validate -> _escalate(baseline) -> stage gate
        |
        +--> SATISFIED?  --- yes --> accept, write JSON, stop
        |
        +--> NOT satisfied
                |
                +--> build a CORRECTION message from the ACTUAL failure
                |       (which key, what was wrong, what is allowed, which
                |        evidence is missing)
                |
                +--> data hint: if the only problem is missing evidence, tell
                |    the model exactly which tool to call next
                |
                +--> NO-PROGRESS GUARD: if the failure signature repeats, stop.
                     Do not burn calls on an identical mistake.

WHAT MAKES THIS SAFE RATHER THAN DANGEROUS
------------------------------------------
1. **Bounded.** ``max_attempts`` (clamped 1..3) and the shared agentic call
   budget both cap the work. A loop cannot drain the OpenRouter free tier.
2. **Deterministic judge.** Correction happens because the *validator* or the
   *stage gate* failed - not because the model thinks it should try again. The
   model never grades its own work.
3. **Monotone safety.** Every attempt passes ``_escalate``, so a correction can
   only ever move toward the safe side. Attempt 3 can never be wider than
   attempt 1's baseline allows.
4. **No-progress guard.** Repeating the same failure signature ends the loop -
   the classic failure mode of self-critique loops is 3 calls that produce the
   same wrong shape.
5. **Escalating hints channel only.** The correction message is built from
   whitelisted fragments (key names, allowed values, missing paths). Model
   output is never echoed back into the next prompt as instructions, so the
   loop cannot be steered by injected text in its own previous answer.
6. **Auditable.** Every attempt is recorded (method, failure, corrected keys),
   so the dashboard can show *why* it took three tries.

WHAT IT NEVER DOES
------------------
* It never writes code or configuration - :class:`SkillWriter` is the only write
  path and it is JSON-only.
* It never places, sizes or cancels an order - no execution module imports it.
* It never clears a veto or widens a limit - ``_escalate`` runs on every attempt.

USAGE
-----
::

    from blue_engine.core.self_correction import SelfCorrectionLoop

    loop = SelfCorrectionLoop(agent, max_attempts=3)
    result = loop.run("risk", text, baseline=risk_baseline, tools=tools)
    # {"ok": True, "advice": {...}, "attempts": [...], "corrections": 2, ...}
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from .frontline_agent import _SCHEMAS, _escalate

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ATTEMPTS_HARD_CAP",
    "CorrectionAttempt",
    "CorrectionResult",
    "SelfCorrectionLoop",
    "diagnose",
    "correction_message",
]

#: A loop longer than this is never worth its quota; clamped at construction.
MAX_ATTEMPTS_HARD_CAP = 3


# --------------------------------------------------------------------------- #
# Diagnosis - WHY did the answer fail?
# --------------------------------------------------------------------------- #
def diagnose(engine: str, payload: Any,
             baseline: Optional[Mapping[str, Any]] = None,
             gate_missing: Optional[List[str]] = None) -> Dict[str, Any]:
    """Return a precise, whitelisted description of what is wrong.

    The output is the ONLY thing allowed into the correction prompt, which is
    what keeps the loop from echoing untrusted text back to the model.

    Returns ``{"kind": ..., "keys": [...], "detail": [...]}`` where ``kind`` is
    ``not_json`` / ``missing_keys`` / ``extra_keys`` / ``bad_value`` /
    ``inconsistent`` / ``clamped`` / ``insufficient_data`` / ``ok``.
    """
    gate_missing = list(gate_missing or [])

    if payload is None:
        return {"kind": "not_json",
                "keys": [],
                "detail": ["the reply was not a single JSON object"]}

    if not isinstance(payload, Mapping):
        return {"kind": "not_json", "keys": [],
                "detail": [f"expected a JSON object, got {type(payload).__name__}"]}

    schema = _SCHEMAS.get(engine) or {}
    expected = set(schema)
    got = {str(k) for k in payload}
    missing = sorted(expected - got)
    extra = sorted(got - expected)

    if missing:
        return {"kind": "missing_keys", "keys": missing,
                "detail": [f"required keys absent: {', '.join(missing)}"]}
    if extra:
        return {"kind": "extra_keys", "keys": extra,
                "detail": [f"unexpected keys present: {', '.join(extra)}"]}

    # Per-key value diagnosis. Mirrors _validate's rules, but reports instead of
    # silently returning None.
    detail: List[str] = []
    for key, rule in schema.items():
        kind = rule[0]
        item = payload.get(key)
        if kind == "enum":
            if type(item) not in (str, int) or item not in rule[1]:
                detail.append(f"{key}: must be one of {list(rule[1])}")
        elif kind == "bool":
            if not isinstance(item, bool):
                detail.append(f"{key}: must be true or false")
        elif kind in ("number", "integer"):
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                detail.append(f"{key}: must be a {kind}")
            else:
                low, high = float(rule[1]), float(rule[2])
                if not low <= float(item) <= high:
                    detail.append(f"{key}: must be between {low} and {high}")
                elif kind == "integer" and int(item) != item:
                    detail.append(f"{key}: must be a whole number")
        elif kind == "text":
            if not isinstance(item, str):
                detail.append(f"{key}: must be a short sentence")
        elif kind == "ticker":
            if not isinstance(item, str) or not item.strip():
                detail.append(f"{key}: must be an uppercase ticker")
        elif kind in ("tickers", "reasons"):
            if not isinstance(item, list):
                detail.append(f"{key}: must be a list")
        elif kind == "date":
            if not isinstance(item, str) or len(item) != 10:
                detail.append(f"{key}: must be a YYYY-MM-DD date")

    if engine == "optimization" and not detail:
        low, high = payload.get("min_exploration_rsi"), payload.get("max_exploration_rsi")
        if isinstance(low, int) and isinstance(high, int) and low > high:
            detail.append("min_exploration_rsi must not exceed max_exploration_rsi")
    if engine == "screener" and not detail:
        listed = set(payload.get("asset_blacklist") or [])
        for row in payload.get("blacklist_reasons") or []:
            if isinstance(row, Mapping) and row.get("ticker") not in listed:
                detail.append("every blacklist_reasons ticker must also appear in "
                              "asset_blacklist")
                break

    if detail:
        return {"kind": "bad_value",
                "keys": sorted({d.split(":")[0] for d in detail}),
                "detail": detail}

    if gate_missing:
        return {"kind": "insufficient_data", "keys": gate_missing,
                "detail": [f"the stage still lacks evidence: "
                           f"{', '.join(gate_missing)}"]}

    if baseline:
        clamped = _escalate(engine, dict(payload), dict(baseline))
        changed = sorted(k for k in clamped
                         if k in payload and clamped[k] != payload[k])
        if changed:
            return {"kind": "clamped", "keys": changed,
                    "detail": [f"{k}: your value was tightened to the safe "
                               f"direction" for k in changed]}

    return {"kind": "ok", "keys": [], "detail": []}


def correction_message(engine: str, diagnosis: Mapping[str, Any],
                       *, tools_available: Optional[List[str]] = None,
                       attempt: int = 1) -> str:
    """Build the correction prompt from whitelisted fragments only."""
    lines = [f"Your previous answer for the '{engine}' stage was REJECTED "
             f"(attempt {attempt})."]
    lines.extend(f"- {item}" for item in diagnosis.get("detail", []))

    if diagnosis.get("kind") == "insufficient_data":
        lines.append("Do not invent a number. Call a read-only tool to fetch the "
                     "missing evidence, then answer again.")
        if tools_available:
            lines.append("Available tools: " + ", ".join(tools_available))
    elif diagnosis.get("kind") == "not_json":
        lines.append("Reply with ONE JSON object and nothing else.")
    else:
        lines.append("Reply with ONE corrected JSON object containing exactly "
                     f"these keys: {', '.join(sorted(_SCHEMAS.get(engine) or {}))}.")
        if tools_available:
            lines.append("You may call a read-only tool first: "
                         + ", ".join(tools_available))
    lines.append("Your answer is an INPUT to deterministic logic, not a trade.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Result records
# --------------------------------------------------------------------------- #
@dataclass
class CorrectionAttempt:
    """One pass of the loop, recorded for the audit trail."""

    attempt: int
    ok: bool
    method: str
    diagnosis: str = ""
    detail: List[str] = field(default_factory=list)
    corrected_keys: List[str] = field(default_factory=list)
    calls: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"attempt": self.attempt, "ok": self.ok, "method": self.method,
                "diagnosis": self.diagnosis, "detail": list(self.detail),
                "corrected_keys": list(self.corrected_keys), "calls": self.calls}


@dataclass
class CorrectionResult:
    """The loop's verdict. ``advice`` is present only when ``ok`` is True."""

    ok: bool
    advice: Optional[Dict[str, Any]] = None
    attempts: List[CorrectionAttempt] = field(default_factory=list)
    corrections: int = 0
    method: str = ""
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "advice": self.advice, "corrections": self.corrections,
                "method": self.method, "warnings": list(self.warnings),
                "attempts": [a.to_dict() for a in self.attempts]}


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #
class SelfCorrectionLoop:
    """Run a stage skill, correct it deterministically, stop when satisfied.

    Parameters
    ----------
    agent : object
        A ``FrontlineAgent``-compatible object exposing ``advise(engine, text,
        baseline=..., tools=...)``.
    max_attempts : int
        Clamped to ``1 .. MAX_ATTEMPTS_HARD_CAP``. ``1`` disables correction.
    stage_gate : object, optional
        A ``StageGate``. When supplied, the gate's verdict is part of the
        acceptance test, so the loop also corrects *insufficient evidence*.
    context : callable, optional
        ``context() -> dict`` returning the current pipeline context; used with
        ``stage_gate`` so each attempt is judged against fresh data.
    gate_stage : str, optional
        The stage name to gate; defaults to the engine name.
    """

    def __init__(self, agent: Any, *, max_attempts: int = 2,
                 stage_gate: Any = None,
                 context: Optional[Callable[[], Mapping[str, Any]]] = None,
                 gate_stage: Optional[str] = None) -> None:
        self.agent = agent
        self.max_attempts = max(1, min(int(max_attempts), MAX_ATTEMPTS_HARD_CAP))
        self.stage_gate = stage_gate
        self._context = context
        self.gate_stage = gate_stage

    # ---- helpers -------------------------------------------------------- #
    def _gate_missing(self, engine: str) -> List[str]:
        if self.stage_gate is None or self._context is None:
            return []
        try:
            verdict = self.stage_gate.check(self.gate_stage or engine,
                                            self._context())
        except Exception as exc:  # noqa: BLE001 - a gate error must not loop
            logger.warning("self_correction: stage gate error: %r", exc)
            return []
        return [] if verdict.satisfied else list(verdict.missing)

    # ---- main ----------------------------------------------------------- #
    def run(self, engine: str, text: str, *,
            baseline: Optional[Mapping[str, Any]] = None,
            tools: Optional[Dict[str, Callable[[Dict[str, Any]], Any]]] = None
            ) -> Dict[str, Any]:
        """Run the loop. Never raises - returns a dict-shaped result."""
        engine = str(engine).lower()
        records: List[CorrectionAttempt] = []
        messages_text = str(text or "")
        last_signature = None
        warnings: List[str] = []
        tool_names = sorted(tools) if tools else None

        for attempt in range(1, self.max_attempts + 1):
            outcome = self.agent.advise(engine, messages_text,
                                        baseline=baseline, tools=tools)
            method = str(outcome.get("method") or "")

            if not outcome.get("ok"):
                diag = {"kind": str(outcome.get("warning") or "not_json"),
                        "keys": [], "detail": [str(outcome.get("warning"))]}
                records.append(CorrectionAttempt(
                    attempt, False, method, diag["kind"], diag["detail"]))
                # A transport/disabled failure is not correctable by re-asking.
                if method in ("disabled", "unsupported", "quota", "llm_failed"):
                    return self._fail(records, method,
                                      warnings + [f"not retryable: {method}"])
                signature = (diag["kind"], tuple(diag["detail"]))
                if signature == last_signature:
                    warnings.append("no progress between attempts; stopping")
                    break
                last_signature = signature
                if attempt < self.max_attempts:
                    messages_text = (str(text or "") + "\n\n"
                                     + correction_message(engine, diag,
                                                          tools_available=tool_names,
                                                          attempt=attempt))
                continue

            advice = outcome.get("advice") or {}
            diag = diagnose(engine, advice, baseline,
                            gate_missing=self._gate_missing(engine))
            records.append(CorrectionAttempt(
                attempt, diag["kind"] == "ok", method, diag["kind"],
                diag["detail"], diag["keys"],
                calls=int(outcome.get("calls") or 0)))

            if diag["kind"] in ("ok", "clamped"):
                # ``clamped`` is a SUCCESS: the safe rails already bound the
                # answer, so re-asking would only spend a call.
                if diag["kind"] == "clamped":
                    warnings.append("advice clamped to the safe direction")
                return {
                    "ok": True,
                    "advice": advice,
                    "attempts": [r.to_dict() for r in records],
                    "corrections": attempt - 1,
                    "method": method,
                    "warnings": warnings + list(outcome.get("warnings") or []),
                    "tool_trace": outcome.get("tool_trace"),
                }

            signature = (diag["kind"], tuple(diag["detail"]))
            if signature == last_signature:
                warnings.append("no progress between attempts; stopping")
                break
            last_signature = signature

            if attempt < self.max_attempts:
                messages_text = (str(text or "") + "\n\n"
                                 + correction_message(engine, diag,
                                                      tools_available=tool_names,
                                                      attempt=attempt))

        last = records[-1] if records else None
        return self._fail(records, last.method if last else "error", warnings)

    @staticmethod
    def _fail(records: List[CorrectionAttempt], method: str,
              warnings: List[str]) -> Dict[str, Any]:
        return {
            "ok": False,
            "advice": None,
            "attempts": [r.to_dict() for r in records],
            "corrections": max(0, len(records) - 1),
            "method": method or "error",
            "warnings": warnings,
        }

    def describe(self) -> Dict[str, Any]:
        return {"max_attempts": self.max_attempts,
                "gated": self.stage_gate is not None,
                "hard_cap": MAX_ATTEMPTS_HARD_CAP}
