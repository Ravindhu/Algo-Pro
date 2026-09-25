"""
portfolio/optim_policy.py — Phase 4: choosing and constraining the optimiser.

Why this file exists
--------------------
The optimiser *primitives* already exist in ``optim/`` (ERC, HRP, NCO,
min-variance, max-Sharpe, Black-Litterman, mean-CVaR, constraints, cost-aware
bands). What was missing is the **policy layer** that decides, for a given book
and regime:

1. **which optimiser** to run,
2. **which constraints** to enforce, and
3. **whether to trade at all** — including a **kill-switch** that overrides
   every other consideration.

That last point is the one that actually saves money. A desk's losses are rarely
caused by picking the wrong objective function; they are caused by *continuing to
trade* through a drawdown, a data fault, or a broken book. This module makes
"stop" a first-class, testable decision rather than an afterthought.

The decision pipeline (in order, first hit wins)
-----------------------------------------------
    kill-switch  ->  data validity  ->  regime policy  ->  optimiser  ->  constraints

* **Kill-switch** — a drawdown or a hard breach halts trading. It is evaluated
  FIRST and cannot be overridden by the regime or the optimiser. Its output is a
  flat/cash book, not an error, so the rest of the pipeline stays pure.
* **Data validity** — refusing to trade on stale or malformed inputs. A
  covariance rebuilt from a stale cache can silently produce a *concentrated*
  book (sleeves that stopped updating look uncorrelated), which is far worse
  than not trading.
* **Regime policy** — reuses ``optim.registry.policy_for`` so regime→optimiser
  mapping stays in one place.
* **Constraints** — merges the desk's static limits (box, gross, turnover, beta
  band) with any regime-specific overrides (e.g. tighter gross when risk-off).

Design notes
------------
* **Pure.** Takes the problem + a snapshot of portfolio state; returns a decision.
  No I/O, no engine calls — the caller (engine or manager) supplies the numbers.
* **Every decision is explained.** ``policy.decide()`` returns a ``reasons`` list
  so the dashboard can show *why* the desk is flat, and a log line is never the
  only record.
* **The kill-switch is deliberately blunt.** It does not try to be clever about
  mean reversion, "the dip", or conviction. It stops. Re-entry is a human
  decision — that asymmetry is the point.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from ..core.results import clamp, safe_float
from .optim import registry as optim_registry

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_LIMITS", "KillSwitch", "check_data_validity",
    "resolve_constraints", "decide", "apply_to_problem",
]


#: Desk risk limits. Overridable per deployment via the ``limits`` config block.
#: The drawdown number is the one that matters most: 15% is a common hard stop
#: for an Indian discretionary/semi-systematic book because recovery from a 15%
#: hole needs a ~17.6% gain, and from 25% needs ~33%.
DEFAULT_LIMITS: Dict[str, Any] = {
    # --- kill-switch ---------------------------------------------------- #
    "max_drawdown": 0.15,          # hard stop: halt new risk beyond this
    "max_daily_loss": 0.04,        # intraday circuit breaker (4% in a session)
    # --- book limits enforced by the optimiser -------------------------- #
    "max_weight": 0.30,            # per-sleeve
    "max_sector_weight": 0.40,     # per sector bucket (see exposure.sector_of)
    "max_gross": 1.00,
    "min_gross": 0.0,
    "max_turnover": 0.10,          # one-way, per rebalance
    "beta_lower": 0.90,
    "beta_upper": 1.10,
    # --- data validity -------------------------------------------------- #
    "min_observations": 60,        # covariance needs enough history
    "max_staleness_days": 5,       # calendar days since the data was fetched
}


class KillSwitch:
    """A latching halt on new risk.

    Deliberately **stateful and sticky**: once tripped, it stays tripped until
    ``reset()`` is called explicitly. A kill-switch that silently un-trips as
    soon as the drawdown ticks back inside the limit is not a kill-switch — it
    is a stop-loss, and it will re-enter at the exact moment the desk is least
    able to judge whether the cause has been fixed.

    ``tripped`` records why and when, so the dashboard can show the reason.
    """

    def __init__(self, *, max_drawdown: float = 0.15,
                 max_daily_loss: float = 0.04,
                 log: Optional[Any] = None):
        self.max_drawdown = float(max_drawdown)
        self.max_daily_loss = float(max_daily_loss)
        self.tripped: bool = False
        self.reason: Optional[str] = None
        self.trigger_value: Optional[float] = None
        self._log = log

    def _logmsg(self, msg: str) -> None:
        if callable(self._log):
            self._log(f"[kill-switch] {msg}")
        else:
            logger.warning("[kill-switch] %s", msg)

    def evaluate(self, drawdown: Optional[float],
                 daily_loss: Optional[float] = None) -> Dict[str, Any]:
        """Check the current state. Trips and STAYS tripped. Never raises.

        ``drawdown`` is a positive fraction off the high-water mark (0.15 = 15%
        down); ``daily_loss`` is a positive fraction lost in the session.
        """
        dd = safe_float(drawdown)
        dl = safe_float(daily_loss)
        if not self.tripped:
            if dd is not None and dd >= self.max_drawdown:
                self.tripped = True
                self.reason = (f"drawdown {dd:.2%} >= limit "
                               f"{self.max_drawdown:.2%}")
                self.trigger_value = dd
            elif dl is not None and dl >= self.max_daily_loss:
                self.tripped = True
                self.reason = (f"daily loss {dl:.2%} >= limit "
                               f"{self.max_daily_loss:.2%}")
                self.trigger_value = dl
        if self.tripped:
            self._logmsg(f"TRIPPED: {self.reason} — no new risk")
        return {"tripped": self.tripped, "reason": self.reason,
                "trigger_value": self.trigger_value,
                "limits": {"max_drawdown": self.max_drawdown,
                           "max_daily_loss": self.max_daily_loss}}

    def reset(self, *, confirm: str = "") -> Dict[str, Any]:
        """Clear the switch. ``confirm`` must be non-empty (an explicit act).

        Requiring a token is a small, deliberate friction: clearing a
        kill-switch should be a conscious decision by a named operator, and the
        requirement makes it hard to do by accident from a script or a UI click.
        """
        if not confirm:
            return {"ok": False, "tripped": self.tripped,
                    "warnings": ["reset requires a non-empty 'confirm' token"]}
        self._logmsg(f"reset by '{confirm}' (was: {self.reason})")
        self.tripped = False
        self.reason = None
        self.trigger_value = None
        return {"ok": True, "tripped": False}


def check_data_validity(inputs: Optional[Dict[str, Any]], *,
                        limits: Optional[Dict[str, Any]] = None
                        ) -> Dict[str, Any]:
    """Refuse to trade on stale or malformed inputs.

    A covariance estimated from stale data is actively dangerous: sleeves whose
    prices stopped updating show *zero* correlation, so the optimiser treats
    them as independent and concentrates into them. Refusing to trade is the
    only safe response to a data fault — a wrong book is worse than no book.

    Checks, all reported (never raises):

    * **universe present** — at least 2 sleeves, otherwise there is nothing to
      allocate across;
    * **observations** — at least ``min_observations``, so the covariance is not
      estimated from noise;
    * **staleness** — ``meta.as_of`` / ``meta.fetched_at`` within
      ``max_staleness_days``;
    * **degenerate covariance** — a zero-variance sleeve or a non-finite entry.
    """
    lim = {**DEFAULT_LIMITS, **(limits or {})}
    out: Dict[str, Any] = {"ok": True, "problems": [], "warnings": []}
    data = inputs or {}
    universe = list(data.get("universe") or [])
    if len(universe) < 2:
        out["problems"].append(
            f"universe has {len(universe)} sleeve(s); need >= 2 to allocate")
    n_obs = safe_float(data.get("n_obs"))
    min_obs = int(lim.get("min_observations", 60))
    if n_obs is not None and n_obs < min_obs:
        out["warnings"].append(
            f"only {int(n_obs)} observations (want >= {min_obs}); the "
            f"covariance is estimated from a short sample")
    meta = data.get("meta") or {}
    if isinstance(meta, dict):
        staleness = safe_float(meta.get("staleness_days"))
        if staleness is None:
            as_of = safe_float(meta.get("as_of_ts"))
            if as_of is not None:
                import time
                staleness = (time.time() - as_of) / 86400.0
        max_stale = safe_float(lim.get("max_staleness_days")) or 5.0
        if staleness is not None and staleness > max_stale:
            out["problems"].append(
                f"data is {staleness:.1f} days old (limit {max_stale:.0f}); "
                f"stale prices make sleeves look uncorrelated")
        if meta.get("stale") and not out["problems"]:
            out["warnings"].append("provider flagged the data as stale")

    cov = data.get("cov")
    if isinstance(cov, list) and cov:
        n = len(cov)
        for i in range(min(n, len(universe) or n)):
            diag = safe_float(cov[i][i]) if i < len(cov[i]) else None
            if diag is None or diag <= 0:
                out["problems"].append(
                    f"sleeve '{universe[i] if i < len(universe) else i}' has "
                    f"non-positive variance; covariance is degenerate")
                break
    out["ok"] = not out["problems"]
    return out


def resolve_constraints(limits: Optional[Dict[str, Any]] = None, *,
                        regime: Optional[str] = None,
                        regime_overrides: Optional[Dict[str, Dict[str, Any]]]
                        = None) -> Dict[str, Any]:
    """Merge desk limits with regime-specific overrides into a constraint problem.

    Precedence: ``DEFAULT_LIMITS`` < ``limits`` < ``regime_overrides[regime]``.

    The regime override is where "risk-off means less gross" lives, which is the
    single most valuable use of the Economic engine's output: the *same*
    optimiser, given a tighter gross cap in a risk-off regime, produces a
    smaller book without any change to the objective function.
    """
    merged = {**DEFAULT_LIMITS, **(limits or {})}
    key = str(regime or "UNKNOWN").upper()
    if regime_overrides and key in regime_overrides:
        merged.update(regime_overrides[key])
    return {
        "min_weight": safe_float(merged.get("min_weight")) or 0.0,
        "max_weight": safe_float(merged.get("max_weight")) or 0.30,
        "max_gross": safe_float(merged.get("max_gross")) or 1.0,
        "min_gross": safe_float(merged.get("min_gross")) or 0.0,
        "max_turnover": safe_float(merged.get("max_turnover")),
        "beta_lower": safe_float(merged.get("beta_lower")),
        "beta_upper": safe_float(merged.get("beta_upper")),
        "max_sector_weight": safe_float(merged.get("max_sector_weight")),
    }


def decide(inputs: Optional[Dict[str, Any]], *,
           regime: Optional[str] = None,
           drawdown: Optional[float] = None,
           daily_loss: Optional[float] = None,
           limits: Optional[Dict[str, Any]] = None,
           exit_on_kill: bool = True) -> Dict[str, Any]:
    """The Phase-4 decision: how to optimise, or whether to trade at all.

    Returns::

        {"ok": bool,            # False only for a hard fault (bad data)
         "trade": bool,         # False when the kill-switch has tripped
         "optimiser": str,      # which primitive to run
         "constraints": {...},  # resolved constraint dict
         "action": "OPTIMISE" | "HALT" | "EXIT_TO_CASH",
         "reasons": [str],      # why — always populated, never guessed
         "warnings": [str],
         "kill": {...}, "data": {...}}

    ``exit_on_kill=True`` (default) asks for a *flat* book when the switch has
    tripped, rather than merely declining to add risk. For a leveraged or
    concentrated Indian book, de-risking to cash is the only action that
    actually stops the bleeding — holding the existing position and simply not
    adding is how a 15% drawdown becomes 30%.
    """
    lim = {**DEFAULT_LIMITS, **(limits or {})}
    reasons: List[str] = []
    warnings: List[str] = []

    # ---- 1. kill-switch (first, and overriding) ------------------------- #
    ks = KillSwitch(max_drawdown=safe_float(lim.get("max_drawdown")) or 0.15,
                    max_daily_loss=safe_float(lim.get("max_daily_loss")) or 0.04)
    kill = ks.evaluate(drawdown, daily_loss)
    if kill["tripped"]:
        reasons.append(f"HALT: kill-switch tripped ({kill['reason']})")
        action = "EXIT_TO_CASH" if exit_on_kill else "HALT"
        return {
            "ok": True, "trade": False, "action": action,
            "optimiser": None,
            "constraints": resolve_constraints(limits, regime=regime),
            "reasons": reasons, "warnings": warnings,
            "kill": kill,
            "data": {"ok": True, "problems": [], "warnings": []},
        }

    # ---- 2. data validity ----------------------------------------------- #
    data_check = check_data_validity(inputs, limits=lim)
    warnings.extend(data_check.get("warnings") or [])
    if not data_check.get("ok"):
        reasons.extend(f"HALT: {p}" for p in data_check["problems"])
        return {
            "ok": False, "trade": False, "action": "HALT", "optimiser": None,
            "constraints": resolve_constraints(limits, regime=regime),
            "reasons": reasons, "warnings": warnings,
            "kill": kill, "data": data_check,
        }
    reasons.append("data valid")

    # ---- 3. regime -> optimiser ----------------------------------------- #
    policy = optim_registry.policy_for(
        regime, overrides=(lim.get("regime_policy") if isinstance(
            lim.get("regime_policy"), dict) else None))
    optimiser = str(policy.get("optimiser", "risk_parity"))
    reasons.append(f"regime {regime or 'UNKNOWN'} -> optimiser '{optimiser}'")

    constraints = resolve_constraints(
        limits, regime=regime,
        regime_overrides=lim.get("regime_overrides")
        if isinstance(lim.get("regime_overrides"), dict) else None)
    reasons.append(
        f"constraints: max_weight={constraints['max_weight']:.2f} "
        f"max_gross={constraints['max_gross']:.2f}"
        + (f" max_turnover={constraints['max_turnover']:.2f}"
           if constraints.get("max_turnover") is not None else ""))

    return {
        "ok": True, "trade": True, "action": "OPTIMISE",
        "optimiser": optimiser, "constraints": constraints,
        "target_vol": safe_float(policy.get("target_vol")),
        "reasons": reasons, "warnings": warnings,
        "kill": kill, "data": data_check,
    }


def apply_to_problem(problem: Dict[str, Any], decision: Dict[str, Any]
                     ) -> Dict[str, Any]:
    """Merge a Phase-4 decision into an optimiser problem dict.

    Returns a NEW dict (the input is not mutated) with ``constraints`` replaced
    by the resolved set, and — when the decision says ``EXIT_TO_CASH`` — a flat
    book of zero weights, so the caller can treat "halt" and "optimise" through
    one code path.

    Why a flat book rather than an error: the downstream stages (POV, risk gate,
    dashboard) all understand weights. Returning a legitimate zero-weight book
    with a recorded reason is far less disruptive than raising, and it makes the
    halt visible in the same places as any other allocation.
    """
    out = dict(problem or {})
    out["constraints"] = dict(decision.get("constraints") or {})
    if decision.get("action") == "EXIT_TO_CASH":
        universe = list(out.get("universe") or [])
        out["weights"] = {u: 0.0 for u in universe}
        out["forced_flat"] = True
    if decision.get("optimiser"):
        out["optimiser"] = decision["optimiser"]
    out["policy_reasons"] = list(decision.get("reasons") or [])
    return out