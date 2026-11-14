"""
optimizer/search_space.py - typed, bounded parameter definitions.

WHY A SEARCH SPACE OBJECT INSTEAD OF A PLAIN DICT
-------------------------------------------------
A dict of parameters is not a search space. A search space also needs to say
what is *legal*, because three separate bugs live in that gap:

1. **Out-of-range values.** ``ema_period = -3`` samples fine in a naive loop and
   then either crashes deep inside a strategy or, worse, silently produces a
   degenerate signal. Bounds catch it at the boundary instead of in the P&L.
2. **Impossible combinations.** ``fast > slow`` on a moving-average crossover
   is not a "bad parameter", it is nonsense. ``validate()`` exists so the
   engine can *reject* rather than score it - a scored nonsense trial pollutes
   the importance ranking for every other parameter.
3. **Unsearchable types.** Ranges that span orders of magnitude (a learning
   rate 1e-4 to 1e-1) must be sampled in LOG space. Sampling them linearly puts
   90% of draws in the top decade and the optimiser never sees the small end.
   That is a *sampling* bug that looks like "the parameter doesn't matter".

THE TRIAL PROTOCOL (duck-typed on purpose)
------------------------------------------
``SearchSpace.sample(trial)`` needs only three methods from whatever asks for
the values:

    trial.suggest_float(name, low, high, log=False)
    trial.suggest_int(name, low, high)
    trial.suggest_categorical(name, choices)

Optuna's ``Trial`` implements exactly this, and so does our fallback
:class:`~blue_engine.optimizer.sampler.LocalTrial`. Keeping to that tiny
protocol is what lets real Optuna drop in later with no change here.

CATEGORICAL PARAMETERS AND IMPORTANCE
-------------------------------------
Importance/Slice/Parallel-coordinate plots all assume the x-axis can be
ordered. That is true for ints and floats and false for categories ("RRG" vs
"GRR"). So every spec declares ``kind``, and ``analysis.py`` renders
categoricals as box plots with an explicit category axis rather than faking a
numeric axis - a fake axis would imply an order that does not exist.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import logger

__all__ = ["ParamSpec", "SearchSpace", "make_space", "ParamValidationError"]


class ParamValidationError(ValueError):
    """Raised when a parameter dict cannot be legal for its space.

    A distinct type (rather than a bare ValueError) so the engine can catch
    *this* and record a rejected trial, instead of swallowing a genuine bug in
    the user's ``validate`` function.
    """


# --------------------------------------------------------------------------- #
# One parameter
# --------------------------------------------------------------------------- #


@dataclass
class ParamSpec:
    """One searchable parameter.

    Args:
        name: key the strategy expects in its config dict.
        kind: ``"float"`` | ``"int"`` | ``"categorical"``.
        low/high: bounds for float/int. Ignored for categorical.
        log: sample log-uniformly. REQUIRED for parameters spanning >2 orders of
            magnitude, otherwise the sampler cannot reach the small end.
        step: optional quantisation (e.g. integers that must be even).
        choices: allowed values for categorical.
        default: the strategy's current/production value. Load-bearing: it is
            what the report compares the recommendation against, and what a
            rejected trial falls back to.
        doc: one line, shown in the dashboard so a reader knows what the knob does.
    """

    name: str
    kind: str
    low: Optional[float] = None
    high: Optional[float] = None
    log: bool = False
    step: Optional[float] = None
    choices: Sequence[Any] = field(default_factory=tuple)
    default: Any = None
    doc: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("float", "int", "categorical"):
            raise ParamValidationError(
                f"{self.name}: kind must be float|int|categorical, got {self.kind!r}")
        if self.kind == "categorical":
            if not self.choices:
                raise ParamValidationError(f"{self.name}: categorical needs choices")
        else:
            if self.low is None or self.high is None:
                raise ParamValidationError(
                    f"{self.name}: {self.kind} needs low and high")
            if not (self.high > self.low):
                raise ParamValidationError(
                    f"{self.name}: high ({self.high}) must exceed low ({self.low})")
            if self.log and self.low <= 0:
                # log(0) is -inf and log(negative) is undefined; a log-scaled
                # bound at or below zero is always a config mistake.
                raise ParamValidationError(
                    f"{self.name}: log scale requires low > 0, got {self.low}")

    # -- sampling ---------------------------------------------------------- #

    def sample(self, trial: Any) -> Any:
        """Draw one value from ``trial`` using the tiny duck-typed protocol."""
        if self.kind == "categorical":
            return trial.suggest_categorical(self.name, list(self.choices))
        if self.kind == "int":
            return int(trial.suggest_int(self.name, int(self.low), int(self.high)))
        if self.log:
            return float(trial.suggest_float(self.name, self.low, self.high, log=True))
        return float(trial.suggest_float(self.name, self.low, self.high))

    # -- validation & coercion --------------------------------------------- #

    def coerce(self, value: Any) -> Any:
        """Return a legal value, snapping to the nearest legal one if close.

        Used on the *recommended* parameter set before it is reported: a
        continuous optimum (e.g. 14.7 bars) must be reported as something the
        strategy can actually be configured with (15 bars). Snapping silently
        would hide a real problem, so callers that care should use
        :meth:`validate` first.
        """
        if value is None:
            return self.default
        if self.kind == "categorical":
            if value in self.choices:
                return value
            return self.default
        try:
            v = float(value)
        except (TypeError, ValueError):
            return self.default
        if not math.isfinite(v):
            return self.default
        # Clamp into range first, then quantise.
        v = min(max(v, float(self.low)), float(self.high))
        if self.step:
            v = round(v / float(self.step)) * float(self.step)
            v = min(max(v, float(self.low)), float(self.high))
        return int(round(v)) if self.kind == "int" else v

    def validate(self, value: Any) -> Tuple[bool, Optional[str]]:
        """Return ``(ok, reason)``. Range/type legality only, no strategy logic."""
        if value is None:
            return False, "missing"
        if self.kind == "categorical":
            if value not in self.choices:
                return False, f"{value!r} not in {list(self.choices)}"
            return True, None
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False, f"{value!r} is not numeric"
        if not math.isfinite(v):
            return False, "not finite"
        if v < float(self.low) or v > float(self.high):
            return False, f"{v} outside [{self.low}, {self.high}]"
        if self.kind == "int" and float(v) != int(v):
            return False, f"{v} is not an integer"
        return True, None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"name": self.name, "kind": self.kind,
                             "default": self.default, "doc": self.doc}
        if self.kind == "categorical":
            d["choices"] = list(self.choices)
        else:
            d.update({"low": self.low, "high": self.high,
                      "log": self.log, "step": self.step})
        return d


# --------------------------------------------------------------------------- #
# The space
# --------------------------------------------------------------------------- #


@dataclass
class SearchSpace:
    """A named, ordered collection of :class:`ParamSpec`.

    ``params`` order is preserved on purpose: it is the order rows appear in
    the dashboard tables, and a stable order makes two runs diffable by eye.
    """

    name: str
    params: List[ParamSpec]
    description: str = ""
    #: Optional cross-parameter rules: ``fn(params) -> None | str``. Returning a
    #: string means "reject this combination, here is why". This is how
    #: ``fast < slow`` is enforced without hardcoding strategy knowledge here.
    constraints: List[Any] = field(default_factory=list)
    #: Free-form metadata carried into the study record (e.g. which strategy
    #: module this space belongs to). Not used by the engine.
    meta: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        seen: set = set()
        for p in self.params:
            if p.name in seen:
                raise ParamValidationError(f"duplicate parameter {p.name!r}")
            seen.add(p.name)
        if not self.params:
            raise ParamValidationError(f"space {self.name!r} has no parameters")

    # -- basics ------------------------------------------------------------ #

    @property
    def names(self) -> List[str]:
        return [p.name for p in self.params]

    @property
    def n_params(self) -> int:
        """Parameter count - the number the AGENTS.md workflow calls "10-param"."""
        return len(self.params)

    def spec(self, name: str) -> ParamSpec:
        for p in self.params:
            if p.name == name:
                return p
        raise KeyError(f"{name!r} is not in space {self.name!r}")

    def defaults(self) -> Dict[str, Any]:
        """The production/baseline parameter set. The bar the search must beat."""
        return {p.name: p.default for p in self.params}

    # -- sampling ---------------------------------------------------------- #

    def sample(self, trial: Any) -> Dict[str, Any]:
        """Draw a full parameter dict from ``trial``.

        TWO STAGES, and the second one is the important one.

        Stage 1 - resample. Draw the whole dict and keep it if the constraints
        pass. Clamping is deliberately NOT used as the primary mechanism: it
        would pile every rejected draw onto a boundary value and teach the
        optimiser that the boundary is unusually good.

        Stage 2 - REPAIR. Rejection sampling collapses when the constraint is
        unsatisfiable in a large part of the box. ``fast < slow`` over
        fast=[2,30], slow=[10,120] rejects roughly a third of the cube, but the
        harder case is a constraint that excludes nearly everything: stage 1 then
        fails every time. The previous behaviour in that situation was to return
        ``defaults()`` - so a large share of the trial budget was silently spent
        re-evaluating ONE identical point, the baseline, and the study could not
        converge because it was not actually searching. Worse, nothing surfaced
        in the report.

        The repair walks the parameter list repeatedly, and for each one asks a
        small set of candidate values whether any of them makes the constraints
        pass. The first candidate that does wins. Candidates are, in order:

            1. the drawn value                       (free - already have it)
            2. the spec's ``default``                (known-legal production value)
            3. the low bound, the high bound, and the midpoint
            4. for numeric specs only: ``default +- step``

        This is a genuine search for a legal point rather than a surrender, and
        it costs at most ``len(params) * len(candidates)`` constraint calls per
        pass - microseconds against a strategy evaluation. If even repair cannot
        find a legal point, the defaults are returned (still legal, so the study
        never crashes) but this is now an ERROR-level event, because it means the
        space's constraints and its bounds disagree - a space-definition bug the
        operator must see, not a routine degrade.
        """
        params = list(self.params)

        # --- stage 1: plain rejection sampling ---------------------------- #
        for _ in range(12):
            cand = {p.name: p.sample(trial) for p in params}
            if self.check_constraints(cand) is None:
                return cand

        # --- stage 2: repair the last draw -------------------------------- #
        cand = {p.name: p.sample(trial) for p in params}
        if self._repair(cand):
            logger.debug("space %s: constraints satisfied after repair", self.name)
            return cand

        logger.error(
            "space %s: no legal parameter set found even after repair; the "
            "constraints reject every candidate including the defaults. "
            "Returning defaults, but this study is NOT searching the space - "
            "fix the space definition. constraints=%s bounds=%s",
            self.name, [getattr(f, "__name__", repr(f)) for f in self.constraints],
            {p.name: (p.low, p.high) if p.kind != "categorical" else list(p.choices)
             for p in params})
        return self.defaults()

    def _candidates_for(self, spec: ParamSpec, drawn: Any) -> List[Any]:
        """Ordered candidate values for one parameter during repair."""
        out: List[Any] = [drawn, spec.default]
        if spec.kind == "categorical":
            out.extend(spec.choices)
        else:
            lo, hi = float(spec.low), float(spec.high)
            out.extend([lo, hi, 0.5 * (lo + hi)])
            if spec.step:
                d = spec.default if spec.default is not None else 0.5 * (lo + hi)
                out.extend([d - spec.step, d + spec.step])
        # Deduplicate while preserving order; None is dropped because it can
        # never validate and only wastes constraint calls.
        seen: List[Any] = []
        for v in out:
            if v is None or any(v == s and type(v) is type(s) for s in seen):
                continue
            seen.append(v)
        return seen

    def _repair(self, cand: Dict[str, Any]) -> bool:
        """Try to make ``cand`` satisfy the constraints, in place.

        Greedy coordinate descent: sweep the parameters, and for each try its
        candidate list in order, accepting the first value that reduces the
        number of violations. Repeat while progress is made, so a constraint
        that needs TWO parameters moved together still converges (moving one at a
        time may not help, but a later pass will see the other one already
        moved).

        Returns True if ``cand`` is legal on exit. Never raises: a constraint
        that throws is counted as a violation by ``check_constraints`` and is
        simply not repairable.
        """
        def n_violations(p: Dict[str, Any]) -> int:
            # ``check_constraints`` reports only the FIRST failure, so count
            # violations directly to give the descent a usable gradient.
            count = 0
            for fn in self.constraints:
                try:
                    if fn(p):
                        count += 1
                except Exception:  # noqa: BLE001 - a broken rule is a violation
                    count += 1
            return count

        if n_violations(cand) == 0:
            return True

        for _ in range(4):                       # bounded: params are few
            improved = False
            for spec in self.params:
                best = n_violations(cand)
                best_val = cand.get(spec.name)
                for val in self._candidates_for(spec, cand.get(spec.name)):
                    trial_val = cand.get(spec.name)
                    cand[spec.name] = val
                    n = n_violations(cand)
                    if n < best:
                        best, best_val, improved = n, val, True
                        if n == 0:
                            cand[spec.name] = val
                            return True
                    cand[spec.name] = trial_val
                cand[spec.name] = best_val
            if not improved:
                break
        return n_violations(cand) == 0

    # -- validation -------------------------------------------------------- #

    def check_constraints(self, params: Dict[str, Any]) -> Optional[str]:
        """Run the cross-parameter rules. ``None`` = acceptable."""
        for fn in self.constraints:
            try:
                reason = fn(params)
            except Exception as exc:  # noqa: BLE001
                # A broken constraint must not kill the study; treat it as a
                # rejection and say so, because silently accepting would let an
                # impossible parameter set through to the report.
                return f"constraint raised {type(exc).__name__}: {exc}"
            if reason:
                return str(reason)
        return None

    def validate(self, params: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Return ``(ok, reasons)`` for a full parameter dict."""
        reasons: List[str] = []
        for p in self.params:
            ok, why = p.validate(params.get(p.name))
            if not ok:
                reasons.append(f"{p.name}: {why}")
        # Extra keys are reported but not fatal: a strategy may accept more
        # config than we search, and refusing to run because of an unrelated
        # passthrough key would be pedantic.
        extra = sorted(set(params) - set(self.names))
        if extra:
            reasons.append(f"extra keys ignored by the space: {extra}")
        c = self.check_constraints(params)
        if c:
            reasons.append(f"constraint: {c}")
        # An "extra keys" note alone is not a failure.
        fatal = [r for r in reasons if not r.startswith("extra keys")]
        return (not fatal), reasons

    def coerce(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Snap a parameter dict onto the space (see :meth:`ParamSpec.coerce`)."""
        return {p.name: p.coerce(params.get(p.name)) for p in self.params}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "n_params": self.n_params,
            "params": [p.to_dict() for p in self.params],
            "meta": dict(self.meta),
        }


# --------------------------------------------------------------------------- #
# Convenience
# --------------------------------------------------------------------------- #


def make_space(name: str, definition: Sequence[Dict[str, Any]], *,
               description: str = "",
               constraints: Optional[Sequence[Any]] = None,
               meta: Optional[Dict[str, Any]] = None) -> SearchSpace:
    """Build a :class:`SearchSpace` from plain dicts (JSON-friendly).

    This is the entry point for the ``param_space.json`` override, so a desk can
    add or tweak a space without editing Python. Each dict is forwarded to
    :class:`ParamSpec`.
    """
    params = [ParamSpec(**dict(d)) for d in definition]
    return SearchSpace(name=name, params=params, description=description,
                       constraints=list(constraints or []), meta=dict(meta or {}))
