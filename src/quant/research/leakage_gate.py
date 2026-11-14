"""Look-ahead leakage gate. Wraps the engine's leak audit; adds a research verdict.

Flow stage: Walk-forward + purged CV, Trial logger + DSR/PBO (research validation lane)
Status: PHASE 5 IMPLEMENTED (wrap; read-only audit, never touches the trade path)
Existing code to reuse or wrap (do not copy):
    src/quant/blue_engine/ml/audit/leakage.py  (check_timestamps,
        check_shift_alignment, check_future_correlation, audit_features)

WHY THIS WRAPPER EXISTS
-----------------------
`src/quant/research/` records every optimisation run in `trial_log.py`, and the
dashboard's Phase-5 optimizer panel shows those runs as evidence. Evidence from
leaked features is worse than no evidence: it looks good and is wrong.

The engine already implements the three checks (timestamps, the behavioural
shift test, the future-correlation screen). This module does NOT re-implement
them. It adds the one thing research needs and the engine does not do:

1. A single **verdict** vocabulary the rest of the system can branch on:
   ``clean`` / ``suspect`` / ``leaked`` / ``incomplete``.
2. A **gate** (`require_clean`) that raises before a backtest is recorded, so a
   leaked study cannot reach the trial log at all.
3. An honest ``incomplete`` state. If the shift test could not run (no
   ``build_features`` callable), the report says so. A skipped check is never
   reported as a pass - that is the whole failure mode this layer exists to stop.

WHAT IT DOES NOT DO
-------------------
- It never writes anything. No file, no registry entry, no order.
- It never imports a model library. ``build_features`` is a callable you pass.
- It never turns a SUSPECT into a failure. Correlation with a forward return is
  a screen, not a proof; the engine returns DEGRADED for a reason.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = [
    "LeakageGateUnavailable",
    "LeakageReport",
    "audit",
    "require_clean",
]

CLEAN = "clean"
SUSPECT = "suspect"
LEAKED = "leaked"
INCOMPLETE = "incomplete"

#: The checks a full audit must have run. A report missing one is INCOMPLETE,
#: never CLEAN, because an unrun check is not a passed check.
REQUIRED_CHECKS = ("timestamps", "shift")


class LeakageGateUnavailable(RuntimeError):
    """The engine's leakage audit cannot be loaded."""


def _module():
    try:
        return load("ml.audit.leakage")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise LeakageGateUnavailable(f"engine leakage audit unavailable: {exc}") from exc


def audit(
    rows: Sequence[Dict[str, Any]],
    *,
    build_features: Optional[Callable[[Sequence[Dict[str, Any]]],
                                      Dict[str, List[Any]]]] = None,
    forward_returns: Optional[Sequence[Optional[float]]] = None,
    ts_col: str = "as_of_ts",
) -> Dict[str, Any]:
    """Run the engine's leak audit and return a report with one clear verdict.

    ``raise_on_fatal`` is forced False here: this function reports, it does not
    throw. Use :func:`require_clean` when you want the gate behaviour. Forcing it
    False is deliberate - a caller passing a bool cannot accidentally turn a
    research report into an exception mid-loop.

    Returns::

        {"verdict": clean|suspect|leaked|incomplete,
         "fatal": int, "suspect": int, "checks_run": [...], "checks_missing": [...],
         "findings": [...], "leaked_features": [...], "detail": str}
    """
    try:
        mod = _module()
    except LeakageGateUnavailable as exc:
        return {
            "verdict": INCOMPLETE, "fatal": 0, "suspect": 0,
            "checks_run": [], "checks_missing": list(REQUIRED_CHECKS),
            "findings": [], "leaked_features": [],
            "detail": f"leakage audit unavailable: {exc}",
        }

    raw = mod.audit_features(
        rows,
        build_features,
        forward_returns,
        ts_col=ts_col,
        raise_on_fatal=False,
    )

    checks_run = list(raw.get("checks_run") or [])
    checks_missing = [c for c in REQUIRED_CHECKS if c not in checks_run]
    fatal = int(raw.get("fatal") or 0)
    suspect = int(raw.get("suspect") or 0)

    if fatal:
        verdict = LEAKED
        detail = (f"{fatal} fatal leak finding(s) on: "
                  f"{', '.join(raw.get('leaked_features') or [])}")
    elif checks_missing:
        # A run with no fatal finding but a skipped check is NOT clean.
        verdict = INCOMPLETE
        detail = f"checks not run: {', '.join(checks_missing)} - cannot certify"
    elif suspect:
        verdict = SUSPECT
        detail = f"{suspect} suspect feature(s) correlated with a forward return; inspect before trusting"
    else:
        verdict = CLEAN
        detail = "all required checks passed"

    return {
        "verdict": verdict,
        "fatal": fatal,
        "suspect": suspect,
        "checks_run": checks_run,
        "checks_missing": checks_missing,
        "findings": list(raw.get("findings") or []),
        "leaked_features": list(raw.get("leaked_features") or []),
        "detail": detail,
    }


def require_clean(report: Dict[str, Any]) -> Dict[str, Any]:
    """Return the report when it is ``clean``. Raise ValueError otherwise.

    Call this immediately before writing a backtest or optimisation result. A
    ``suspect`` report is refused too: the rule is that unproven evidence is not
    recorded as evidence. To record a study you have inspected by hand, audit it
    again with the real ``build_features`` so the shift test actually runs.
    """
    verdict = str(report.get("verdict") or "")
    if verdict == CLEAN:
        return report
    raise ValueError(
        f"leakage gate refused (verdict={verdict or 'unknown'}): "
        f"{report.get('detail') or 'no detail'}")
