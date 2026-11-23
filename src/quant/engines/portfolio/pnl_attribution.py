"""
portfolio/pnl_attribution.py — Phase 3b: where did the P&L actually come from?

Why this file exists
--------------------
``brinson.py`` answers "allocation or selection?" against a benchmark. That is
the right question for a mandate review, but it is not the question a trading
desk asks at 16:00: *which sleeve, which strategy, and which part of the session
made or lost the money?* This module answers that.

Two decompositions, both computed from the fill log Phase 1 already records:

1. **By sleeve** — realised P&L per sleeve, with fees shown separately so gross
   edge and net edge are never confused. A sleeve with positive gross and
   negative net is a *cost* problem, not a signal problem, and the two fixes are
   completely different.
2. **By time-of-day bucket** — India microstructure matters here:
   * **OPEN (09:15–10:00)** — price discovery, widest spreads, highest volume.
     A strategy that makes money here is usually paying for it in impact.
   * **MID (10:00–14:00)** — the least informative, thinnest book.
   * **CLOSE (14:00–15:30)** — institutional positioning, volume ramp, expiry
     effects. Most retail alpha in Indian equities shows up in this bucket.
   * **AFTER (15:30+)** — off-market; should be empty for cash equity. A
     non-zero bucket here almost always means a timestamp bug or a
     timezone error, so it is surfaced rather than silently folded into CLOSE.

3. **By strategy** — when fills carry a ``strategy`` label (the POV engine sets
   one), P&L is grouped by it, so multi-strategy books can be judged separately.

Design notes
------------
* **Pure.** Takes the fill list; returns dicts. No I/O.
* **Attribution must reconcile.** ``sum(by_sleeve) == total`` and
  ``sum(by_bucket) == total`` are asserted in tests. An attribution table that
  does not add up to the P&L is worse than no table — someone will make a
  decision on the gap.
* **Gross and net are both reported.** Never one without the other.
* **Timestamps are IST.** The bucket boundaries only mean anything in IST; a UTC
  fill log would put the entire morning session in "AFTER".

India cost note: fees are the ``Fill.charges`` already computed per side by
``tracker`` (STT + brokerage + GST + stamp + slippage), so the net column is the
number that matches the broker's contract note.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..core.results import safe_float

logger = logging.getLogger(__name__)

__all__ = ["IST", "BUCKETS", "bucket_for", "by_sleeve", "by_bucket",
           "by_strategy", "attribute", "reconcile"]

IST = timezone(timedelta(hours=5, minutes=30))

#: Session buckets in IST: ``(name, start_hhmm, end_hhmm)``. End is exclusive
#: except for the last bucket, which absorbs everything after 15:30 (see module
#: docstring: a non-empty AFTER bucket is a red flag, not a feature).
BUCKETS: Tuple[Tuple[str, int, int], ...] = (
    ("OPEN", 9 * 60 + 15, 10 * 60),        # 09:15-10:00 price discovery
    ("MID", 10 * 60, 14 * 60),             # 10:00-14:00 thin book
    ("CLOSE", 14 * 60, 15 * 60 + 30),      # 14:00-15:30 institutional flow
    ("AFTER", 15 * 60 + 30, 24 * 60),      # should be empty for cash equity
)


def bucket_for(ts: Optional[float]) -> str:
    """Session bucket name for an epoch timestamp, evaluated in IST.

    A fill before the open (or a nonsense timestamp) is reported as ``PRE``
    rather than being silently classified as OPEN — pre-open and opening-auction
    fills have completely different economics from continuous-session ones.
    """
    stamp = safe_float(ts)
    if stamp is None:
        return "UNKNOWN"
    try:
        local = datetime.fromtimestamp(stamp, tz=IST)
    except (OverflowError, OSError, ValueError):
        return "UNKNOWN"
    if local.weekday() >= 5:
        return "WEEKEND"
    minutes = local.hour * 60 + local.minute
    if minutes < BUCKETS[0][1]:
        return "PRE"
    for name, start, end in BUCKETS:
        if start <= minutes < end:
            return name
    return "AFTER"


# --------------------------------------------------------------------------- #
# Decompositions
# --------------------------------------------------------------------------- #

def _blank(name: str) -> Dict[str, Any]:
    return {"group": name, "realized_pnl": 0.0, "fees": 0.0,
            "net_pnl": 0.0, "fills": 0, "volume": 0}


def _add(bucket: Dict[str, Any], pnl: float, fees: float, qty: int) -> None:
    bucket["realized_pnl"] += pnl
    bucket["fees"] += fees
    bucket["net_pnl"] = bucket["realized_pnl"] - bucket["fees"]
    bucket["fills"] += 1
    bucket["volume"] += abs(int(qty))


def _realised_per_fill(tracker: Any) -> List[Tuple[Any, float, float]]:
    """Pair each fill with the realised P&L and fee it caused.

    ``EquityTracker.on_fill`` accrues realised P&L on the *lot*, so a fill's own
    contribution is the change in that lot's realised total. We therefore need
    the tracker to have exposed its fills; anything else would require replaying
    the whole book.
    """
    out: List[Tuple[Any, float, float]] = []
    fills = list(tracker.fills() or [])
    # Rebuild per-fill realised deltas by walking the fills in order and
    # tracking each sleeve's cumulative realised P&L from the tracker's own
    # per-lot totals is not possible retroactively, so we recompute the running
    # total the same way the tracker does: average-cost accounting.
    running: Dict[str, Tuple[int, float, float]] = {}   # key -> (qty, avg, rpnl)
    for f in fills:
        key = f"{f.symbol}|{f.exchange}"
        qty, avg, cum = running.get(key, (0, 0.0, 0.0))
        delta = 0.0
        q = int(f.qty)
        px = float(f.price)
        if f.side == "BUY":
            if qty >= 0:
                new_qty = qty + q
                avg = ((qty * avg) + q * px) / new_qty if new_qty else 0.0
                qty = new_qty
            else:
                closing = min(q, abs(qty))
                delta += (avg - px) * closing
                qty += q
                if qty > 0:
                    avg = px
                elif qty == 0:
                    avg = 0.0
        else:
            if qty > 0:
                closing = min(q, qty)
                delta += (px - avg) * closing
                qty -= q
                if qty < 0:
                    avg = px
                elif qty == 0:
                    avg = 0.0
            else:
                new_qty = abs(qty) + q
                avg = ((abs(qty) * avg) + q * px) / new_qty if new_qty else 0.0
                qty -= q
        running[key] = (qty, avg, cum + delta)
        out.append((f, delta, float(f.charges)))
    return out


def by_sleeve(tracker: Any) -> Dict[str, Any]:
    """Realised P&L per sleeve, gross and net of fees.

    ``net`` is the number that matters: a sleeve can be gross-positive and
    net-negative when turnover is high, which is a *cost* problem to fix (wider
    no-trade band, slower cadence) rather than a signal problem.
    """
    groups: Dict[str, Dict[str, Any]] = {}
    total_gross = total_fees = 0.0
    for f, pnl, fee in _realised_per_fill(tracker):
        g = groups.setdefault(f.symbol, _blank(f.symbol))
        _add(g, pnl, fee, f.qty)
        total_gross += pnl
        total_fees += fee
    ordered = dict(sorted(groups.items(), key=lambda kv: kv[1]["net_pnl"],
                          reverse=True))
    for g in ordered.values():
        g["realized_pnl"] = round(g["realized_pnl"], 4)
        g["fees"] = round(g["fees"], 4)
        g["net_pnl"] = round(g["net_pnl"], 4)
    return {"ok": bool(ordered), "groups": ordered,
            "total_realized": round(total_gross, 4),
            "total_fees": round(total_fees, 4),
            "total_net": round(total_gross - total_fees, 4),
            "warnings": []}


def by_bucket(tracker: Any) -> Dict[str, Any]:
    """Realised P&L per IST session bucket (OPEN / MID / CLOSE / AFTER)."""
    groups: Dict[str, Dict[str, Any]] = {}
    total_gross = total_fees = 0.0
    for f, pnl, fee in _realised_per_fill(tracker):
        name = bucket_for(getattr(f, "ts", None))
        g = groups.setdefault(name, _blank(name))
        _add(g, pnl, fee, f.qty)
        total_gross += pnl
        total_fees += fee
    # Present the buckets in session order, not by P&L, so the desk can read the
    # day left-to-right; AFTER/PRE stay visible on purpose (red flags).
    order = ["PRE", "OPEN", "MID", "CLOSE", "AFTER", "WEEKEND", "UNKNOWN"]
    ordered = {k: groups[k] for k in order if k in groups}
    warnings: List[str] = []
    after = groups.get("AFTER")
    if after and after["fills"]:
        warnings.append(
            f"{after['fills']} fill(s) booked AFTER 15:30 IST — impossible for "
            f"cash equity; check for a timestamp or timezone bug")
    for g in ordered.values():
        g["realized_pnl"] = round(g["realized_pnl"], 4)
        g["fees"] = round(g["fees"], 4)
        g["net_pnl"] = round(g["net_pnl"], 4)
    return {"ok": bool(ordered), "buckets": ordered,
            "total_realized": round(total_gross, 4),
            "total_fees": round(total_fees, 4),
            "total_net": round(total_gross - total_fees, 4),
            "warnings": warnings}


def by_strategy(tracker: Any, *, strategy_of: Optional[Any] = None
                ) -> Dict[str, Any]:
    """Realised P&L per strategy label.

    ``strategy_of`` is an optional callable ``fill -> str``. When the fill log
    does not carry a strategy (the default), every fill lands in ``"unlabelled"``
    and the caller is warned — better a visible gap than a fabricated split.
    """
    if strategy_of is None:
        strategy_of = lambda f: getattr(f, "strategy", None) or "unlabelled"  # noqa: E731
    groups: Dict[str, Dict[str, Any]] = {}
    total_gross = total_fees = 0.0
    for f, pnl, fee in _realised_per_fill(tracker):
        name = str(strategy_of(f) or "unlabelled")
        g = groups.setdefault(name, _blank(name))
        _add(g, pnl, fee, f.qty)
        total_gross += pnl
        total_fees += fee
    ordered = dict(sorted(groups.items(), key=lambda kv: kv[1]["net_pnl"],
                          reverse=True))
    for g in ordered.values():
        g["realized_pnl"] = round(g["realized_pnl"], 4)
        g["fees"] = round(g["fees"], 4)
        g["net_pnl"] = round(g["net_pnl"], 4)
    warnings: List[str] = []
    if list(ordered) == ["unlabelled"]:
        warnings.append("no strategy labels on the fills; all P&L in "
                        "'unlabelled'")
    return {"ok": bool(ordered), "groups": ordered,
            "total_realized": round(total_gross, 4),
            "total_fees": round(total_fees, 4),
            "total_net": round(total_gross - total_fees, 4),
            "warnings": warnings}


def reconcile(section: Dict[str, Any], *, key: str) -> Dict[str, Any]:
    """Check that a decomposition's groups sum back to its own total.

    This is the guard that keeps the attribution honest. Returns
    ``{"ok", "residual", "tolerance"}`` where residual is the unexplained
    difference; anything above a cent per sleeve is a bug, not rounding.
    """
    groups = section.get(key) or {}
    total = safe_float(section.get("total_net")) or 0.0
    summed = sum(safe_float(g.get("net_pnl")) or 0.0 for g in groups.values())
    residual = summed - total
    tol = max(0.01, abs(total) * 1e-9)
    return {"ok": abs(residual) <= tol, "residual": round(residual, 6),
            "summed": round(summed, 4), "total": round(total, 4),
            "tolerance": tol}


def attribute(tracker: Any, *, strategy_of: Optional[Any] = None
              ) -> Dict[str, Any]:
    """Full P&L attribution: by sleeve, by session bucket, by strategy.

    Never raises. Each section is reconciled against its own total, and any
    reconciliation failure is surfaced in ``warnings`` (and sets ``ok=False``)
    so a table that does not add up is never presented as fact.
    """
    try:
        sleeve = by_sleeve(tracker)
        bucket = by_bucket(tracker)
        strategy = by_strategy(tracker, strategy_of=strategy_of)
        warnings: List[str] = list(bucket.get("warnings") or [])
        warnings.extend(strategy.get("warnings") or [])
        checks = {
            "by_sleeve": reconcile(sleeve, key="groups"),
            "by_bucket": reconcile(bucket, key="buckets"),
            "by_strategy": reconcile(strategy, key="groups"),
        }
        bad = [name for name, c in checks.items() if not c["ok"]]
        for name in bad:
            warnings.append(
                f"{name} does not reconcile (residual "
                f"{checks[name]['residual']:.4f}); totals may be inconsistent")
        return {
            "ok": not bad,
            "by_sleeve": sleeve,
            "by_bucket": bucket,
            "by_strategy": strategy,
            "reconciliation": checks,
            "total_realized": sleeve.get("total_realized"),
            "total_fees": sleeve.get("total_fees"),
            "total_net": sleeve.get("total_net"),
            "n_fills": len(list(tracker.fills() or [])),
            "warnings": warnings,
        }
    except Exception as exc:  # noqa: BLE001 - a report must never break a page
        logger.warning("pnl_attribution.attribute failed: %r", exc)
        return {"ok": False, "by_sleeve": {}, "by_bucket": {},
                "by_strategy": {}, "reconciliation": {},
                "warnings": [f"attribution failed: {exc}"]}