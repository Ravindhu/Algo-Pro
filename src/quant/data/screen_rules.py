"""Three-valued screening rules (PASS / FAIL / UNKNOWN). Local code, no Blue import.

Flow stage: Screener rules (stage 6, sub-stage 1)
Status: PHASE 1 IMPLEMENTED 2026-11-06 (moved from src/quant/blue_engine/screener/filters.py)
Ported from: src/quant/blue_engine/screener/filters.py (Rule, RuleResult, ScreenResult, Screener,
default_rules) and the part of src/quant/blue_engine/screener/client/numbers.py that filters use
(``to_number`` with no unit scale). The ``unit_scale="absolute"`` branch of ``to_number``
is NOT ported: no screen rule calls it.
Parity: tests/parity/test_screen_rules_parity.py. Hand-checked: tests/unit/test_screen_rules.py.

WHY THREE VALUES
----------------
A rule returns PASS, FAIL or UNKNOWN. UNKNOWN (metric missing or unparseable) is not a pass
and not a silent drop: it is reported with a reason. With the default ``on_missing="fail"``,
a name whose risk cannot be measured is not selected ("if we cannot measure the risk, we do
not take the position", AGENTS.md capital-protection order).

SECTOR GATE
-----------
Banks, NBFCs and insurers report debt/equity that is not a solvency measure. The default
``exclude_sectors`` drops them before the debt rule runs. Do not loosen the debt rule for
them; write a different rule.

NO I/O
------
The screen takes ``(symbol, metrics)`` candidates and returns verdicts. It knows nothing
about screener.in, so the same screen works on any fundamentals feed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# One number parser for the whole screener: screener_numbers.py (no duplicate copies).
from quant.data.screener_numbers import to_number

logger = logging.getLogger(__name__)

PASS = "pass"
FAIL = "fail"
UNKNOWN = "unknown"

#: Sectors excluded from the debt rule by default (case-insensitive substring match).
DEFAULT_EXCLUDE_SECTORS = ["bank", "finance", "insurance", "nbfc"]

# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


@dataclass
class Rule:
    """One rule over one metric. ``on_missing``: "fail" (default), "pass" (ignore) or "unknown"."""

    metric: str
    op: str
    threshold: float
    label: str = ""
    on_missing: str = "fail"
    unit: str = ""

    def __post_init__(self) -> None:
        if self.op not in _OPS:
            raise ValueError(f"rule {self.metric!r}: unknown op {self.op!r}; valid: {sorted(_OPS)}")
        if self.on_missing not in ("fail", "pass", "unknown"):
            raise ValueError(f"rule {self.metric!r}: on_missing must be fail/pass/unknown")
        self.threshold = float(self.threshold)
        if not self.label:
            self.label = f"{self.metric} {self.op} {self.threshold:g}"

    def evaluate(self, metrics: Dict[str, Any]) -> "RuleResult":
        value = to_number(metrics.get(self.metric))
        if value is None:
            if self.on_missing == "pass":
                return RuleResult(self.metric, PASS, None, self.label,
                                  "metric missing; rule ignored by policy")
            kind = UNKNOWN if self.on_missing == "unknown" else FAIL
            return RuleResult(self.metric, kind, None, self.label,
                              "metric missing or unparseable")
        ok = _OPS[self.op](value, self.threshold)
        return RuleResult(
            self.metric, PASS if ok else FAIL, value, self.label,
            "" if ok else f"{value:g} does not satisfy {self.op} {self.threshold:g}",
        )

    @classmethod
    def from_dict(cls, spec: Dict[str, Any]) -> "Rule":
        """Accepts {"metric","op","threshold"} or sugar {"metric","min"|"min_inclusive"|"max"|"max_inclusive"|"equals"}."""
        spec = dict(spec or {})
        common = dict(label=str(spec.get("label") or ""),
                      on_missing=str(spec.get("on_missing") or "fail"),
                      unit=str(spec.get("unit") or ""))
        if "op" in spec and "threshold" in spec:
            return cls(metric=str(spec["metric"]), op=str(spec["op"]),
                       threshold=float(spec["threshold"]), **common)
        metric = str(spec.get("metric") or "")
        for op, key in ((">=", "min_inclusive"), (">", "min"),
                        ("<=", "max_inclusive"), ("<", "max"), ("==", "equals")):
            if key in spec:
                return cls(metric=metric, op=op, threshold=float(spec[key]), **common)
        raise ValueError(f"rule spec for {metric!r} has no op/threshold/min/max")


@dataclass
class RuleResult:
    metric: str
    outcome: str
    value: Optional[float]
    label: str
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome == PASS

    def as_dict(self) -> Dict[str, Any]:
        return {"metric": self.metric, "outcome": self.outcome, "value": self.value,
                "label": self.label, "reason": self.reason}


@dataclass
class ScreenResult:
    symbol: str
    selected: bool
    rules: List[RuleResult] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    excluded_by: Optional[str] = None
    name: str = ""

    @property
    def failed_rules(self) -> List[RuleResult]:
        return [r for r in self.rules if r.outcome == FAIL]

    @property
    def unknown_rules(self) -> List[RuleResult]:
        return [r for r in self.rules if r.outcome == UNKNOWN]

    def reason(self) -> str:
        if self.excluded_by:
            return self.excluded_by
        if self.selected:
            return "passed all rules"
        bad = self.failed_rules + self.unknown_rules
        if not bad:
            return "not selected"
        return "; ".join(f"{r.label}: {r.reason}" for r in bad[:3])

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "name": self.name, "selected": self.selected,
                "metrics": self.metrics, "rules": [r.as_dict() for r in self.rules],
                "reason": self.reason()}


class Screener:
    """A named set of rules over candidate dicts ``{"symbol", "metrics", "name"?, "sector"?}``."""

    def __init__(self, rules: List[Rule], *, name: str = "screen",
                 min_rules_passed: Optional[int] = None,
                 exclude_sectors: Optional[List[str]] = None,
                 exclude_symbols: Optional[List[str]] = None,
                 require_all: bool = True):
        if not rules:
            raise ValueError("a screen needs at least one rule")
        self.rules = list(rules)
        self.name = name
        self.min_rules_passed = min_rules_passed
        self.require_all = require_all
        self.exclude_sectors = [s.lower() for s in (exclude_sectors or [])]
        self.exclude_symbols = {s.upper() for s in (exclude_symbols or [])}

    def _sector_gate(self, sector: Optional[str]) -> Optional[str]:
        if not sector or not self.exclude_sectors:
            return None
        s = str(sector).lower()
        for bad in self.exclude_sectors:
            if bad and bad in s:
                return (f"sector {sector!r} excluded "
                        f"(debt/equity is not meaningful for this sector)")
        return None

    def evaluate(self, candidates: List[Dict[str, Any]]) -> List[ScreenResult]:
        out: List[ScreenResult] = []
        for cand in candidates:
            symbol = str(cand.get("symbol") or "").upper()
            metrics = cand.get("metrics") or {}
            res = ScreenResult(symbol=symbol, selected=False, metrics=metrics,
                               name=str(cand.get("name") or ""))
            if not symbol:
                res.excluded_by = "candidate has no symbol"
            elif symbol in self.exclude_symbols:
                res.excluded_by = "symbol on the explicit exclude list"
            else:
                res.excluded_by = self._sector_gate(cand.get("sector"))
            if res.excluded_by:
                out.append(res)
                continue
            res.rules = [rule.evaluate(metrics) for rule in self.rules]
            passed = sum(1 for r in res.rules if r.passed)
            if self.min_rules_passed is not None:
                res.selected = passed >= self.min_rules_passed
            else:
                res.selected = (passed == len(self.rules)) if self.require_all else passed > 0
            out.append(res)
        n_sel = sum(1 for r in out if r.selected)
        n_unknown = sum(1 for r in out if r.unknown_rules)
        logger.info("[screen:%s] %d candidates -> %d selected, %d with missing data",
                    self.name, len(out), n_sel, n_unknown)
        return out

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "Screener":
        """Build from a config block. A missing ``exclude_sectors`` key means the default deny list;
        an explicit ``[]`` turns the gate off."""
        cfg = dict(cfg or {})
        rules = [Rule.from_dict(r) for r in (cfg.get("rules") or [])]
        if not rules:
            rules = default_rules(min_market_cap_cr=cfg.get("min_market_cap_cr"),
                                  max_debt_equity=cfg.get("max_debt_equity"))
        exclude_sectors = cfg.get("exclude_sectors")
        if exclude_sectors is None:
            exclude_sectors = list(DEFAULT_EXCLUDE_SECTORS)
        return cls(rules=rules, name=str(cfg.get("name") or "default"),
                   min_rules_passed=(int(cfg["min_rules_passed"])
                                     if cfg.get("min_rules_passed") is not None else None),
                   exclude_sectors=exclude_sectors, exclude_symbols=cfg.get("exclude_symbols"),
                   require_all=bool(cfg.get("require_all", True)))


def default_rules(*, min_market_cap_cr: Optional[float] = None,
                  max_debt_equity: Optional[float] = None) -> List[Rule]:
    """The requested screen: market cap > 200 Cr AND debt/equity < 1 (both strict)."""
    mc = 200.0 if min_market_cap_cr is None else float(min_market_cap_cr)
    de = 1.0 if max_debt_equity is None else float(max_debt_equity)
    return [
        Rule(metric="market_cap", op=">", threshold=mc, unit="cr",
             label=f"Market cap > {mc:g} Cr", on_missing="fail"),
        Rule(metric="debt_equity", op="<", threshold=de, unit="ratio",
             label=f"Debt/Equity < {de:g}", on_missing="fail"),
    ]


__all__ = ["PASS", "FAIL", "UNKNOWN", "DEFAULT_EXCLUDE_SECTORS", "Rule", "RuleResult",
           "ScreenResult", "Screener", "default_rules", "to_number"]
