"""Improvement loop: compare a challenger against the champion and RECOMMEND only.

Flow stage: Champion/challenger, Re-opt request
Status: PHASE 4 IMPLEMENTED (advice only; never promotes, never resizes)

WHAT THIS MODULE DOES
---------------------
Given the champion's out-of-sample metrics and a challenger's, it answers one
question: *is the challenger good enough that a human should look at it?*

It returns a verdict plus the reasons. It does **not** call
``ModelRegistry.promote``. Promotion needs a named human sign-off (AGENTS.md:
"Only approved models enter production... A person signs off"), so the loop's
output is a piece of evidence and a recommendation, never a state change.

WHY A SEPARATE GATE FROM promote()
----------------------------------
``ModelRegistry.promote`` already refuses an unsigned model and a model whose
DSR is below the champion's. That is the *action* gate and it must stay dumb and
hard. This module is the *judgement* gate: it exists so the dashboard and the
research runner can say "challenger X looks better, here is why" without anyone
being tempted to wire promote() straight into a cron job.

NO AUTO-PROMOTION, EVER
-----------------------
``recommend()`` returns ``promote_recommended: bool``. Nothing in this file
reads that flag. A test asserts promote() is not called from here.

WHY THE RULES ARE THESE RULES
-----------------------------
1. **Enough evidence.** A challenger with fewer than ``MIN_OOS_OBS`` out-of-
   sample observations is rejected whatever its Sharpe. A spectacular 8-trade
   sample is noise, and a loop that promotes on it is a curve-fitting machine.
2. **DSR must not regress.** Deflated Sharpe accounts for the number of trials,
   so it is the honest comparator after an optimisation run (AGENTS.md:
   "An optimization that is not in the trial log does not count as evidence").
3. **Improvement must be material.** A 0.01 Sharpe "win" is inside the noise of
   any real backtest. ``MIN_SHARPE_GAIN`` sets the bar.
4. **PBO is a veto.** A challenger with a high probability of backtest
   overfitting is rejected even if it scores better - that is precisely the case
   PBO exists to catch.

Everything is advisory: the caller decides what to do with the verdict.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: Below this many out-of-sample observations the verdict is always "insufficient".
MIN_OOS_OBS = 100
#: A challenger must beat the champion's Sharpe by at least this much.
MIN_SHARPE_GAIN = 0.25
#: Above this probability of backtest overfitting the challenger is rejected.
MAX_PBO = 0.50
#: DSR tolerance: a challenger may not be worse than the champion on this.
MIN_DSR_DELTA = 0.0


@dataclass
class Comparison:
    """The verdict. Fields are plain data so it can be audited and plotted."""

    verdict: str                      # "promote_recommended" | "keep_champion" | "insufficient"
    promote_recommended: bool
    reasons: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "promote_recommended": self.promote_recommended,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
            "advice_only": True,
        }


def _num(metrics: Dict[str, Any], key: str) -> Optional[float]:
    """Read a metric as a finite float, or None. Never raises on bad input."""
    value = metrics.get(key)
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):   # NaN / inf
        return None
    return out


def compare(champion: Dict[str, Any], challenger: Dict[str, Any],
            *, min_oos_obs: int = MIN_OOS_OBS,
            min_sharpe_gain: float = MIN_SHARPE_GAIN,
            max_pbo: float = MAX_PBO) -> Comparison:
    """Judge a challenger against the champion. Returns advice, never a promotion.

    ``champion`` and ``challenger`` are metric dicts. Recognised keys:
    ``sharpe``, ``dsr``, ``pbo``, ``n_oos``. Missing keys are treated as
    "unknown" and block a positive recommendation rather than passing silently.
    """
    reasons: List[str] = []

    # ---- 1. evidence size ------------------------------------------------ #
    n_oos = _num(challenger, "n_oos")
    if n_oos is None or n_oos < min_oos_obs:
        reasons.append(
            f"insufficient out-of-sample evidence: n_oos={n_oos} < {min_oos_obs}")
        return Comparison("insufficient", False, reasons,
                          {"champion": dict(champion), "challenger": dict(challenger)})

    # ---- 2. required metrics present ------------------------------------- #
    c_sharpe = _num(challenger, "sharpe")
    ch_sharpe = _num(champion, "sharpe")
    if c_sharpe is None or ch_sharpe is None:
        reasons.append("sharpe missing or not a finite number for champion or challenger")
        return Comparison("insufficient", False, reasons,
                          {"champion": dict(champion), "challenger": dict(challenger)})

    # ---- 3. PBO veto ----------------------------------------------------- #
    pbo = _num(challenger, "pbo")
    if pbo is None:
        reasons.append("pbo missing: cannot rule out backtest overfitting")
        return Comparison("insufficient", False, reasons,
                          {"champion": dict(champion), "challenger": dict(challenger)})
    if pbo > max_pbo:
        reasons.append(f"PBO {pbo:.2f} above {max_pbo:.2f}: overfitting risk rejects it")
        return Comparison("keep_champion", False, reasons,
                          {"champion": dict(champion), "challenger": dict(challenger)})

    # ---- 4. DSR must not regress ----------------------------------------- #
    c_dsr = _num(challenger, "dsr")
    ch_dsr = _num(champion, "dsr")
    if c_dsr is None or ch_dsr is None:
        reasons.append("dsr missing: deflated Sharpe is required after an optimisation")
        return Comparison("insufficient", False, reasons,
                          {"champion": dict(champion), "challenger": dict(challenger)})
    if c_dsr < ch_dsr + MIN_DSR_DELTA:
        reasons.append(f"DSR regresses: challenger {c_dsr:.3f} < champion {ch_dsr:.3f}")
        return Comparison("keep_champion", False, reasons,
                          {"champion": dict(champion), "challenger": dict(challenger)})

    # ---- 5. material improvement ----------------------------------------- #
    gain = c_sharpe - ch_sharpe
    if gain < min_sharpe_gain:
        reasons.append(
            f"gain {gain:+.3f} below the {min_sharpe_gain:.2f} bar: inside backtest noise")
        return Comparison("keep_champion", False, reasons,
                          {"champion": dict(champion), "challenger": dict(challenger)})

    reasons.append(
        f"challenger better: sharpe {ch_sharpe:.3f} -> {c_sharpe:.3f} (+{gain:.3f}), "
        f"DSR {ch_dsr:.3f} -> {c_dsr:.3f}, PBO {pbo:.2f}, n_oos {int(n_oos)}")
    reasons.append("advice only: promotion still needs a named human sign-off")
    return Comparison("promote_recommended", True, reasons,
                      {"champion": dict(champion), "challenger": dict(challenger)})


def evaluate_and_record(strategy_id: str, champion: Dict[str, Any],
                        challenger: Dict[str, Any], audit=None,
                        **kwargs) -> Comparison:
    """``compare()`` plus one audit row. The audit row is the permanent evidence."""
    result = compare(champion, challenger, **kwargs)
    if audit is not None:
        audit.append("challenger_compared",
                     {"strategy_id": strategy_id, **result.to_dict()})
    return result


class ImprovementLoop:
    """Run the challenger comparison for a list of strategies and collect the advice.

    Nothing here mutates a registry, a strategy or a position. It produces a
    report a person reads before deciding anything.
    """

    def __init__(self, audit: Optional[object] = None) -> None:
        self.audit = audit
        self.results: List[Dict[str, Any]] = []

    def run(self, candidates: List[Dict[str, Any]], **kwargs) -> List[Dict[str, Any]]:
        """``candidates`` is a list of {"strategy_id", "champion", "challenger"} dicts."""
        self.results = []
        for item in candidates:
            strategy_id = str(item.get("strategy_id", "unknown"))
            result = evaluate_and_record(
                strategy_id, item.get("champion") or {}, item.get("challenger") or {},
                audit=self.audit, **kwargs)
            self.results.append({"strategy_id": strategy_id, **result.to_dict()})
        return self.results

    def summary(self) -> Dict[str, int]:
        """Counts by verdict, for the dashboard header."""
        counts: Dict[str, int] = {"promote_recommended": 0,
                                  "keep_champion": 0, "insufficient": 0}
        for row in self.results:
            counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
        return counts
