"""
stage_gate.py - the DATA SUFFICIENCY gate between pipeline stages.

WHY THIS FILE EXISTS
====================
The pipeline (``core/pipeline.py``) has a *safety* gate (infrastructure
readiness + risk veto) but no *evidence* gate: a stage can run with an empty
payload and the pipeline happily advances. This module adds the missing rule
the operator asked for:

    "only data-satisfied logic code moves to the next stage"

So a stage declares the evidence it needs, this gate reads that evidence, and
the pipeline stops the analytic chain when the evidence is not there.

DESIGN RULES (mirroring ``core/base.py``)
-----------------------------------------
* PURE AND DETERMINISTIC. No network, no broker SDK, no LLM, no writes. It only
  reads the context dict the pipeline already assembled.
* FAIL-CLOSED. A requirement that cannot be evaluated is a *block*, not a pass.
  That is the whole point: an unverifiable input must never unlock the next
  stage.
* NEVER RAISES. A malformed requirement is reported as a failed check.
* NO SILENT FABRICATION. A missing number is reported as missing; the gate never
  substitutes a default value.

WHAT "SATISFIED" MEANS
----------------------
A stage is satisfied when every one of its requirements passes. Six checks are
supported, all evaluated against a *dotted path* into the context:

============  =============================================================
check         passes when
============  =============================================================
``present``   the path resolves to a non-empty value
``min_rows``  the path resolves to a sequence of at least ``value`` items
``min_count`` the path resolves to a number >= ``value``
``range``     the path resolves to a finite number in ``[min, max]``
``one_of``    the path resolves to a value in the ``value`` list
``enum_keys`` the path is a dict containing every key in ``value``
============  =============================================================

An optional ``"max_age_seconds"`` on any requirement makes it also require a
fresh timestamp: the path named by ``"timestamp_path"`` must parse to a
timezone-aware ISO timestamp within that age. Without a ``timestamp_path`` the
freshness check is ignored (and the requirement is reported as
``freshness_unverifiable`` only when ``require_freshness`` is true).

USAGE
-----
::

    from blue_engine.core.stage_gate import StageGate, check_requirements

    gate = StageGate.from_config(config)
    verdict = gate.check("economic", ctx)
    if not verdict.satisfied:
        ...   # do NOT advance

Config shape (``engines.stage_gate`` in ``input/macro_master.json``)::

    "stage_gate": {
      "enabled": true,
      "on_block": "halt_analysis",          # halt_analysis | warn
      "stages": {
        "economic": [
          {"path": "macro.policy_rates", "check": "min_count", "value": 1},
          {"path": "macro.cpi", "check": "present"}
        ]
      }
    }
"""
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "StageGateError",
    "Requirement",
    "RequirementResult",
    "StageVerdict",
    "StageGate",
    "DEFAULT_REQUIREMENTS",
    "resolve_path",
    "check_requirements",
]

#: Outcomes the pipeline may use for a blocked stage.
ON_BLOCK_MODES = ("halt_analysis", "warn")


class StageGateError(ValueError):
    """Raised only for a *configuration* error at construction time."""


# --------------------------------------------------------------------------- #
# Default, deliberately conservative requirement map
# --------------------------------------------------------------------------- #
#: These are the minimum evidence each stage needs to be meaningfully runnable.
#: Every requirement is overridable from config; nothing here invents a number.
#: The ``sector``/``screener``/``valuation`` stages are NOT listed because they
#: accept an empty universe legitimately (an empty screen is a valid answer).
DEFAULT_REQUIREMENTS: Dict[str, List[Dict[str, Any]]] = {
    "economic": [
        {"path": "macro", "check": "present",
         "note": "macro snapshot: policy rate, CPI, growth inputs"},
    ],
    "market": [
        {"path": "market", "check": "present",
         "note": "market snapshot: index quote + volatility state"},
    ],
    "portfolio": [
        {"path": "alpha.data.selected", "check": "present",
         "note": "selecting a portfolio needs at least one signal"},
    ],
    "risk": [
        {"path": "portfolio.data", "check": "present",
         "note": "risk sizing needs the portfolio engine's output"},
    ],
}


def resolve_path(context: Mapping[str, Any], path: str) -> Tuple[bool, Any]:
    """Resolve a dotted path into ``context``.

    A segment may carry an explicit dict key (the common case). Returns
    ``(found, value)``; ``found`` is False when any segment is absent.

    The traversal treats a mapping segment as a key lookup. A list segment that
    is a plain decimal integer indexes the list (``"rows.0.close"``). This keeps
    the gate usable on real provider payloads without adding a query language.
    """
    if not isinstance(context, Mapping) or not path:
        return False, None
    node: Any = context
    for segment in str(path).split("."):
        if isinstance(node, Mapping):
            if segment not in node:
                return False, None
            node = node[segment]
        elif isinstance(node, (list, tuple)) and segment.isdigit():
            index = int(segment)
            if index >= len(node):
                return False, None
            node = node[index]
        else:
            return False, None
    return True, node


# --------------------------------------------------------------------------- #
# Requirement + result dataclasses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Requirement:
    """One evidence requirement for one stage."""

    path: str
    check: str = "present"
    value: Any = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    timestamp_path: Optional[str] = None
    max_age_seconds: Optional[float] = None
    require_freshness: bool = False
    note: str = ""

    CHECKS = ("present", "min_rows", "min_count", "range", "one_of", "enum_keys")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], *, where: str = "") -> "Requirement":
        if not isinstance(raw, Mapping):
            raise StageGateError(f"{where}: requirement must be a mapping")
        path = raw.get("path")
        if not isinstance(path, str) or not path.strip():
            raise StageGateError(f"{where}: requirement needs a non-empty 'path'")
        check = str(raw.get("check") or "present")
        if check not in cls.CHECKS:
            raise StageGateError(
                f"{where}: unknown check {check!r} for path {path!r}")
        min_age = raw.get("max_age_seconds")
        min_age = None if min_age is None else _as_float(min_age, where="max_age_seconds")
        return cls(
            path=path.strip(),
            check=check,
            value=raw.get("value"),
            min_value=_opt_float(raw.get("min_value"), where),
            max_value=_opt_float(raw.get("max_value"), where),
            timestamp_path=(str(raw["timestamp_path"])
                            if raw.get("timestamp_path") else None),
            max_age_seconds=min_age,
            require_freshness=bool(raw.get("require_freshness", False)),
            note=str(raw.get("note") or ""),
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "path": self.path, "check": self.check, "value": self.value,
            "min_value": self.min_value, "max_value": self.max_value,
            "timestamp_path": self.timestamp_path,
            "max_age_seconds": self.max_age_seconds,
            "require_freshness": self.require_freshness, "note": self.note,
        }


@dataclass(frozen=True)
class RequirementResult:
    """The verdict for a single requirement."""

    path: str
    check: str
    passed: bool
    reason: str
    observed: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "check": self.check, "passed": self.passed,
                "reason": self.reason, "observed": _jsonable(self.observed)}


@dataclass
class StageVerdict:
    """The verdict for one stage - satisfied only when every check passed."""

    stage: str
    satisfied: bool
    checks: List[RequirementResult] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "satisfied": self.satisfied,
            "reason": self.reason,
            "missing": list(self.missing),
            "checks": [c.to_dict() for c in self.checks],
        }

    def __bool__(self) -> bool:  # ``if verdict:`` == ``if verdict.satisfied``
        return self.satisfied


# --------------------------------------------------------------------------- #
# Small numeric helpers (never raise)
# --------------------------------------------------------------------------- #
def _opt_float(value: Any, where: str = "") -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise StageGateError(f"{where}: numeric bound must not be a bool")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise StageGateError(f"{where}: not a number: {value!r}") from exc
    if not math.isfinite(out):
        raise StageGateError(f"{where}: numeric bound must be finite")
    return out


def _as_float(value: Any, where: str = "") -> float:
    out = _opt_float(value, where)
    if out is None:
        raise StageGateError(f"{where}: value is required")
    return out


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _is_sequence(value: Any) -> bool:
    return isinstance(value, (list, tuple, set, frozenset))


def _jsonable(value: Any) -> Any:
    """Best-effort JSON-safe echo of an observed value for the report."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (list, tuple, set)):
        return f"<{type(value).__name__} len={len(value)}>"
    if isinstance(value, Mapping):
        keys = list(value.keys())[:8]
        return f"<mapping keys={keys}{'...' if len(value) > 8 else ''}>"
    return f"<{type(value).__name__}>"


def _parse_timestamp(value: Any) -> Optional[_dt.datetime]:
    """Parse an ISO timestamp into an aware datetime, else None."""
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #
def _check_present(req: Requirement, value: Any) -> Tuple[bool, str]:
    if value is None:
        return False, "missing"
    if isinstance(value, str) and not value.strip():
        return False, "empty string"
    if _is_sequence(value) or isinstance(value, Mapping):
        if len(value) == 0:
            return False, "empty collection"
    return True, "present"


def _check_min_rows(req: Requirement, value: Any) -> Tuple[bool, str]:
    if not _is_sequence(value):
        return False, f"not a sequence ({type(value).__name__})"
    need = _as_float(req.value, where=f"{req.path}.value")
    if len(value) < need:
        return False, f"{len(value)} rows < required {int(need)}"
    return True, f"{len(value)} rows"


def _check_min_count(req: Requirement, value: Any) -> Tuple[bool, str]:
    if not _is_finite_number(value):
        return False, f"not a finite number ({_jsonable(value)})"
    need = _as_float(req.value, where=f"{req.path}.value")
    if float(value) < need:
        return False, f"{float(value)} < required {need}"
    return True, f"{float(value)} >= {need}"


def _check_range(req: Requirement, value: Any) -> Tuple[bool, str]:
    if not _is_finite_number(value):
        return False, f"not a finite number ({_jsonable(value)})"
    number = float(value)
    if req.min_value is not None and number < req.min_value:
        return False, f"{number} < min {req.min_value}"
    if req.max_value is not None and number > req.max_value:
        return False, f"{number} > max {req.max_value}"
    return True, f"{number} in range"


def _check_one_of(req: Requirement, value: Any) -> Tuple[bool, str]:
    allowed = req.value
    if not isinstance(allowed, (list, tuple, set, frozenset)):
        return False, "requirement 'value' must be a list for check 'one_of'"
    if value not in allowed:
        return False, f"{_jsonable(value)} not in {list(allowed)}"
    return True, "allowed value"


def _check_enum_keys(req: Requirement, value: Any) -> Tuple[bool, str]:
    if not isinstance(value, Mapping):
        return False, f"not a mapping ({type(value).__name__})"
    required = req.value
    if not isinstance(required, (list, tuple, set, frozenset)):
        return False, "requirement 'value' must be a list for check 'enum_keys'"
    missing = [str(k) for k in required if k not in value]
    if missing:
        return False, f"missing keys: {missing}"
    return True, "all keys present"


_CHECKERS = {
    "present": _check_present,
    "min_rows": _check_min_rows,
    "min_count": _check_min_count,
    "range": _check_range,
    "one_of": _check_one_of,
    "enum_keys": _check_enum_keys,
}


def _check_freshness(req: Requirement, context: Mapping[str, Any],
                     now: Optional[_dt.datetime]) -> Tuple[bool, str]:
    """Apply the optional freshness rule for one requirement."""
    if req.max_age_seconds is None and not req.require_freshness:
        return True, ""
    if not req.timestamp_path:
        # Fail-closed: "we need fresh data" with no timestamp source cannot be
        # verified, so it is a block rather than a silent pass.
        return False, "freshness required but no timestamp_path configured"
    found, raw_ts = resolve_path(context, req.timestamp_path)
    if not found:
        return False, f"timestamp path {req.timestamp_path!r} missing"
    parsed = _parse_timestamp(raw_ts)
    if parsed is None:
        return False, (f"timestamp {req.timestamp_path!r} is not a timezone-aware "
                       f"ISO string")
    if req.max_age_seconds is None:
        return True, "timestamp present"
    current = now or _dt.datetime.now(_dt.timezone.utc)
    age = (current - parsed).total_seconds()
    if age > req.max_age_seconds:
        return False, (f"data age {age:.0f}s > max {req.max_age_seconds:.0f}s")
    if age < -60.0:
        return False, f"timestamp is {abs(age):.0f}s in the future (clock skew)"
    return True, f"age {age:.0f}s"


def check_requirements(requirements: Iterable[Requirement],
                       context: Mapping[str, Any], *,
                       now: Optional[_dt.datetime] = None) -> Tuple[bool, List[RequirementResult]]:
    """Evaluate every requirement. Returns ``(all_passed, results)``.

    Never raises: a requirement that cannot be evaluated is recorded as a
    failure with the reason, which is the fail-closed behaviour.
    """
    results: List[RequirementResult] = []
    all_passed = True
    for req in requirements:
        try:
            found, value = resolve_path(context, req.path)
            if not found:
                results.append(RequirementResult(
                    req.path, req.check, False, "path not found", None))
                all_passed = False
                continue
            passed, reason = _CHECKERS[req.check](req, value)
            observed = value
            if passed:
                fresh_ok, fresh_reason = _check_freshness(req, context, now)
                if not fresh_ok:
                    passed, reason = False, fresh_reason
                elif fresh_reason:
                    reason = f"{reason}; {fresh_reason}"
            results.append(RequirementResult(req.path, req.check, passed,
                                             reason, observed))
            all_passed = all_passed and passed
        except Exception as exc:  # noqa: BLE001 - the gate must never raise
            results.append(RequirementResult(
                req.path, req.check, False,
                f"requirement could not be evaluated: {exc!r}", None))
            all_passed = False
    return all_passed, results


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
class StageGate:
    """Read-only data-sufficiency gate for the analytic pipeline.

    Constructed once from config; ``check()`` is called by the pipeline before
    each stage. Disabled at construction when ``enabled`` is false - a disabled
    gate returns a satisfied verdict so the pipeline behaves exactly as before.
    """

    def __init__(self, config: Optional[Mapping[str, Any]] = None) -> None:
        cfg = dict(config or {})
        self.enabled: bool = bool(cfg.get("enabled", False))
        mode = str(cfg.get("on_block") or "halt_analysis")
        if mode not in ON_BLOCK_MODES:
            raise StageGateError(
                f"stage_gate.on_block must be one of {ON_BLOCK_MODES}, got {mode!r}")
        self.on_block: str = mode

        raw_stages = cfg.get("stages")
        if raw_stages is None:
            raw_stages = DEFAULT_REQUIREMENTS if self.enabled else {}
        if not isinstance(raw_stages, Mapping):
            raise StageGateError("stage_gate.stages must be a mapping")

        self.requirements: Dict[str, List[Requirement]] = {}
        for stage, entries in raw_stages.items():
            name = str(stage).strip().lower()
            if not name:
                raise StageGateError("stage_gate.stages has an empty stage name")
            if entries is None:
                continue
            if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
                raise StageGateError(
                    f"stage_gate.stages[{name!r}] must be a list of requirements")
            self.requirements[name] = [
                Requirement.from_dict(entry, where=f"stage_gate.stages[{name!r}]")
                for entry in entries
            ]

    # ---- constructors --------------------------------------------------- #
    @classmethod
    def from_config(cls, config: Optional[Mapping[str, Any]]) -> "StageGate":
        """Build from the whole pipeline config (reads ``engines.stage_gate``)."""
        engines = (config or {}).get("engines") if isinstance(config, Mapping) else None
        block = (engines or {}).get("stage_gate") if isinstance(engines, Mapping) else None
        return cls(block or {})

    # ---- checks --------------------------------------------------------- #
    def has_requirements(self, stage: str) -> bool:
        return bool(self.requirements.get(str(stage).strip().lower()))

    def check(self, stage: str, context: Mapping[str, Any], *,
              now: Optional[_dt.datetime] = None) -> StageVerdict:
        """Return the verdict for ``stage``. Never raises."""
        name = str(stage).strip().lower()
        if not self.enabled:
            return StageVerdict(name, True, [], [], "stage gate disabled")
        reqs = self.requirements.get(name)
        if not reqs:
            # No declared requirement == nothing to verify. Reported explicitly
            # so an operator can see the stage is ungated rather than assume it
            # was checked.
            return StageVerdict(name, True, [], [], "no requirements declared")

        satisfied, results = check_requirements(reqs, context, now=now)
        missing = [r.path for r in results if not r.passed]
        if satisfied:
            reason = f"{len(results)} requirement(s) satisfied"
        else:
            reason = "insufficient data: " + ", ".join(missing)
        return StageVerdict(name, satisfied, results, missing, reason)

    def describe(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "on_block": self.on_block,
            "stages": {k: [r.describe() for r in v]
                       for k, v in sorted(self.requirements.items())},
        }
