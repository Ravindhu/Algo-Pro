"""
filters.py - the screening rules that turn raw fundamentals into a tradable list.

THE TWO RULES REQUESTED
-----------------------
    1. market cap  >  200 Cr
    2. debt/equity <  1

They are declared in ``screener_filters.json``, not hard-coded, but they are the
defaults and the ones the tests assert.

THE ONE DESIGN DECISION THAT MATTERS
------------------------------------
A rule is a **three-valued** predicate, not a boolean. For each candidate it
returns one of:

    PASS    - the metric is present and satisfies the threshold
    FAIL    - the metric is present and violates the threshold
    UNKNOWN - the metric is missing / unparseable / the cell said "-"

UNKNOWN is NOT a pass and NOT a silent drop. It is reported separately, with a
reason, so the operator can see *why* a name was excluded. This matters
enormously for the debt rule: a company that has not reported borrowings would,
under a naive ``debt_equity = 0.0`` default, sail through "< 1" — the filter
would select precisely the names whose debt you cannot see.

So the default policy is ``on_missing = "fail"``: **if we cannot measure the
risk, we do not take the position.** That is the capital-protection ordering
from AGENTS.md, applied at the screening layer.

SECTOR / DEBT SPECIAL CASE (READ THIS BEFORE CHANGING THE RULE)
---------------------------------------------------------------
Banks, NBFCs and insurance companies legitimately show a **blank or absurdly
high** debt/equity on screener.in. Their "borrowings" ARE their inventory. Two
consequences, both handled here:

* A blank debt/equity on a Financials name is NOT the same evidence as a blank
  debt/equity on a manufacturer. ``exclude_sectors`` (default: banking, finance,
  insurance) drops them before the debt rule is even applied, which is the
  standard professional treatment and avoids the rule silently mislabelling
  book-driven businesses.
* ``debt_equity_max`` is compared against the *reported* value. We do not
  rescale to a "leverage" definition. If you want banks in, write a different
  rule — do not loosen this one.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .client.numbers import to_number

logger = logging.getLogger(__name__)

#: Outcomes of one rule evaluation.
PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"

#: Sectors excluded from the debt rule by default. Banks, NBFCs and insurers
#: report debt/equity that is not a solvency measure - their borrowings ARE
#: their inventory - so applying "D/E < 1" to them selects the opposite of what
#: the screen intends. Matching is a case-insensitive substring against the
#: sector label, so "Banks", "Private Banks" and "Banking" all hit.
DEFAULT_EXCLUDE_SECTORS = ["bank", "finance", "insurance", "nbfc"]


# --------------------------------------------------------------------------- #
# Rule objects
# --------------------------------------------------------------------------- #
@dataclass
class Rule:
    """One screening rule over one metric.

    Attributes:
        metric: canonical metric key (see ``numbers.RATIO_ALIASES`` plus ``debt_equity``).
        op: one of ``>``, ``>=``, ``<``, ``<=``, ``==``, ``!=``.
        threshold: the comparison value.
        label: human-readable description, shown in the UI and the audit row.
        on_missing: what to do when the metric is absent -
            ``"fail"`` (default; exclude and record a reason) or
            ``"pass"`` (ignore the rule for this name) or
            ``"unknown"`` (exclude, but tag the reason as "data missing"
            so it is distinguishable from a genuine violation).
        unit: passthrough note for the reader only (``"cr"`` / ``"ratio"`` /
            ``"pct"``); the *actual* unit decision is made by the caller when it
            fills the metric dict. Kept so a rule is self-documenting.
    """

    metric: str
    op: str
    threshold: float
    label: str = ""
    on_missing: str = "fail"
    unit: str = ""

    #: Ops, mapped to the comparison. Kept as a dict so an unknown op is a
    #: construction-time error, not a silently-skipped rule.
    _OPS = {
        ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b,
        "<": lambda a, b: a < b,
        "<=": lambda a, b: a <= b,
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
    }

    def __post_init__(self) -> None:
        if self.op not in self._OPS:
            raise ValueError(
                f"rule {self.metric!r}: unknown op {self.op!r}; "
                f"valid: {sorted(self._OPS)}")
        self.threshold = float(self.threshold)
        if not self.label:
            self.label = f"{self.metric} {self.op} {self.threshold:g}"

    def evaluate(self, metrics: Dict[str, Any]) -> "RuleResult":
        """Apply the rule to one candidate's metric dict."""
        raw = metrics.get(self.metric)
        value = to_number(raw)

        if value is None:
            # Three distinct missing cases collapse here on purpose: key absent,
            # cell blank, cell "-". The caller does not need to tell them apart;
            # it needs to know the measurement did not happen.
            if self.on_missing == "pass":
                return RuleResult(self.metric, PASS, None, self.label,
                                  "metric missing; rule ignored by policy")
            kind = UNKNOWN if self.on_missing == "unknown" else FAIL
            return RuleResult(self.metric, kind, None, self.label,
                              "metric missing or unparseable")

        ok = self._OPS[self.op](value, self.threshold)
        outcome = PASS if ok else FAIL
        return RuleResult(
            self.metric, outcome, value, self.label,
            "" if ok else f"{value:g} does not satisfy {self.op} {self.threshold:g}",
        )

    @classmethod
    def from_dict(cls, spec: Dict[str, Any]) -> "Rule":
        """Build from JSON, tolerating the common shorthand forms.

        Accepts ``{"metric": "market_cap", "min": 200}`` as sugar for
        ``{">": 200}`` because that is how a human writes a screen.
        """
        spec = dict(spec or {})
        if "op" in spec and "threshold" in spec:
            return cls(
                metric=str(spec["metric"]),
                op=str(spec["op"]),
                threshold=float(spec["threshold"]),
                label=str(spec.get("label") or ""),
                on_missing=str(spec.get("on_missing") or "fail"),
                unit=str(spec.get("unit") or ""),
            )
        metric = str(spec.get("metric") or "")
        for op, key in ((">=", "min_inclusive"), (">", "min"),
                        ("<=", "max_inclusive"), ("<", "max"),
                        ("==", "equals")):
            if key in spec:
                return cls(
                    metric=metric, op=op,
                    threshold=float(spec[key]),
                    label=str(spec.get("label") or ""),
                    on_missing=str(spec.get("on_missing") or "fail"),
                    unit=str(spec.get("unit") or ""),
                )
        raise ValueError(f"rule spec for {metric!r} has no op/threshold/min/max")


@dataclass
class RuleResult:
    """The outcome of one rule against one candidate."""

    metric: str
    outcome: str
    value: Optional[float]
    label: str
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome == PASS

    def as_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "outcome": self.outcome,
            "value": self.value,
            "label": self.label,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# The screen
# --------------------------------------------------------------------------- #
@dataclass
class ScreenResult:
    """Verdict for one symbol after every rule has run."""

    symbol: str
    selected: bool
    rules: List[RuleResult] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    excluded_by: Optional[str] = None      # sector gate, fetch failure, ...
    name: str = ""

    @property
    def failed_rules(self) -> List[RuleResult]:
        return [r for r in self.rules if r.outcome == FAIL]

    @property
    def unknown_rules(self) -> List[RuleResult]:
        return [r for r in self.rules if r.outcome == UNKNOWN]

    def reason(self) -> str:
        """One-line human explanation of the verdict (used by the dashboard)."""
        if self.excluded_by:
            return self.excluded_by
        if self.selected:
            return "passed all rules"
        bad = self.failed_rules + self.unknown_rules
        if not bad:
            return "not selected"
        return "; ".join(f"{r.label}: {r.reason}" for r in bad[:3])

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "selected": self.selected,
            "metrics": self.metrics,
            "rules": [r.as_dict() for r in self.rules],
            "reason": self.reason(),
        }


class Screener:
    """A named set of rules over candidate metric dicts.

    The screen has no I/O and no knowledge of screener.in. It is handed a list
    of ``(symbol, metrics)`` pairs and returns verdicts. That separation is what
    makes the exact same screen testable offline and reusable on any other
    fundamental feed (an OpenAlgo provider, a CSV dump, a broker API).
    """

    def __init__(self, rules: List[Rule], *,
                 name: str = "screen",
                 min_rules_passed: Optional[int] = None,
                 exclude_sectors: Optional[List[str]] = None,
                 exclude_symbols: Optional[List[str]] = None,
                 require_all: bool = True):
        if not rules:
            raise ValueError("a screen needs at least one rule")
        self.rules = list(rules)
        self.name = name
        #: When set, a candidate passes if it satisfies this many rules. Default
        #: (None) means ALL rules must pass, which is what the requested screen
        #: does - "market cap > 200 CR AND debt/equity < 1" is a conjunction.
        self.min_rules_passed = min_rules_passed
        self.require_all = require_all
        self.exclude_sectors = [s.lower() for s in (exclude_sectors or [])]
        self.exclude_symbols = {s.upper() for s in (exclude_symbols or [])}

    # -- gate helpers ---------------------------------------------------- #
    def _sector_gate(self, sector: Optional[str]) -> Optional[str]:
        """Return an exclusion reason when the sector is on the deny list."""
        if not sector or not self.exclude_sectors:
            return None
        s = str(sector).lower()
        for bad in self.exclude_sectors:
            if bad and bad in s:
                return (f"sector {sector!r} excluded "
                        f"(debt/equity is not meaningful for this sector)")
        return None

    # -- main ------------------------------------------------------------- #
    def evaluate(self, candidates: List[Dict[str, Any]]) -> List[ScreenResult]:
        """Run the screen over candidates.

        Each candidate is a dict with at least ``symbol`` and ``metrics``;
        optionally ``name`` and ``sector``. Candidates that fail a gate are
        returned with ``excluded_by`` set so nothing disappears silently.
        """
        out: List[ScreenResult] = []
        for cand in candidates:
            symbol = str(cand.get("symbol") or "").upper()
            metrics = cand.get("metrics") or {}
            result = ScreenResult(
                symbol=symbol, selected=False, metrics=metrics,
                name=str(cand.get("name") or ""),
            )

            if not symbol:
                result.excluded_by = "candidate has no symbol"
                out.append(result)
                continue

            if symbol in self.exclude_symbols:
                result.excluded_by = "symbol on the explicit exclude list"
                out.append(result)
                continue

            gate = self._sector_gate(cand.get("sector"))
            if gate:
                result.excluded_by = gate
                out.append(result)
                continue

            for rule in self.rules:
                result.rules.append(rule.evaluate(metrics))

            passed = sum(1 for r in result.rules if r.passed)
            if self.min_rules_passed is not None:
                result.selected = passed >= self.min_rules_passed
            else:
                result.selected = (passed == len(self.rules)) if self.require_all \
                    else passed > 0
            out.append(result)

        n_sel = sum(1 for r in out if r.selected)
        n_unknown = sum(1 for r in out if r.unknown_rules)
        logger.info("[screen:%s] %d candidates -> %d selected, %d with missing data",
                    self.name, len(out), n_sel, n_unknown)
        return out

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "rules": [{"metric": r.metric, "op": r.op,
                       "threshold": r.threshold, "label": r.label,
                       "on_missing": r.on_missing} for r in self.rules],
            "min_rules_passed": self.min_rules_passed,
            "require_all": self.require_all,
            "exclude_sectors": self.exclude_sectors,
            "exclude_symbols": sorted(self.exclude_symbols),
        }

    # -- construction ---------------------------------------------------- #
    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "Screener":
        """Build from the ``screen`` block of ``screener_engine.json``.

        Supports two styles, because both are natural:

            "rules": [ {"metric": "market_cap", "op": ">", "threshold": 200}, ... ]
            "rules": [ {"metric": "market_cap", "min": 200}, ... ]

        Sector exclusions DEFAULT to the financial-sector deny list, matching the
        shipped ``screener_engine.json``. The code default MUST equal the shipped
        config: leaving financials in means the debt rule is applied to lenders,
        whose "borrowings" are their inventory, and the screen then reports the
        opposite of what it claims. Set ``exclude_sectors: []`` explicitly to
        turn the gate off - an empty list is a deliberate choice, a missing key
        is not.
        """
        cfg = dict(cfg or {})
        raw_rules = cfg.get("rules") or []
        rules = [Rule.from_dict(r) for r in raw_rules]
        if not rules:
            rules = default_rules(
                min_market_cap_cr=cfg.get("min_market_cap_cr"),
                max_debt_equity=cfg.get("max_debt_equity"),
            )
        exclude_sectors = cfg.get("exclude_sectors")
        if exclude_sectors is None:
            exclude_sectors = list(DEFAULT_EXCLUDE_SECTORS)
        return cls(
            rules=rules,
            name=str(cfg.get("name") or "default"),
            min_rules_passed=(int(cfg["min_rules_passed"])
                              if cfg.get("min_rules_passed") is not None else None),
            exclude_sectors=exclude_sectors,
            exclude_symbols=cfg.get("exclude_symbols"),
            require_all=bool(cfg.get("require_all", True)),
        )


# --------------------------------------------------------------------------- #
# The requested screen
# --------------------------------------------------------------------------- #
def default_rules(*, min_market_cap_cr: Optional[float] = None,
                  max_debt_equity: Optional[float] = None) -> List[Rule]:
    """The requested two-rule screen.

        1. market cap  >  200 Cr
        2. debt/equity <  1

    Both are strict inequalities, matching the request. A company sitting
    exactly at 200 Cr or exactly at debt/equity 1.0 is therefore EXCLUDED.
    """
    mc = 200.0 if min_market_cap_cr is None else float(min_market_cap_cr)
    de = 1.0 if max_debt_equity is None else float(max_debt_equity)
    return [
        Rule(
            metric="market_cap", op=">", threshold=mc, unit="cr",
            label=f"Market cap > {mc:g} Cr",
            on_missing="fail",
        ),
        Rule(
            metric="debt_equity", op="<", threshold=de, unit="ratio",
            label=f"Debt/Equity < {de:g}",
            on_missing="fail",
        ),
    ]


__all__ = [
    "PASS", "FAIL", "UNKNOWN",
    "DEFAULT_EXCLUDE_SECTORS",
    "Rule", "RuleResult", "ScreenResult", "Screener", "default_rules",
]
