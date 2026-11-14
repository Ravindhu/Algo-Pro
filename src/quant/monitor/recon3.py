"""Three-way reconciliation: our ledger vs OpenAlgo vs the broker.

Flow stage: Ledger and recon
Status: PHASE 3 IMPLEMENTED (2026-11-14 three-way leg)
Existing code to reuse or wrap (do not copy):
    quant.monitor.recon_job (run_recon - the two-way ledger vs broker job, kept)
    quant.monitor.ledger (Ledger.recon - position comparison)

WHY A THIRD LEG
---------------
v5 2.10 says "Ledger and recon: Intent ID trail, 3-way match". Only TWO legs
existed (our ledger vs the broker). The middle leg - what OpenAlgo believes it
has - was never compared.

That middle leg is where the failures actually show up. OpenAlgo sits between us
and the broker: a dropped WebSocket update, a partially filled order we never saw,
or a manual trade from the OpenAlgo UI all leave OpenAlgo and the broker
disagreeing while our ledger agrees with neither. A two-way check can only say
"we differ from the broker". Three-way says WHICH leg drifted, which is the
difference between an afternoon of investigation and a one-line fix.

WHAT IT DOES
------------
Takes three position maps and returns, per symbol, every leg that disagrees plus a
verdict naming the likely cause:

  * ``ours_missing``     - we hold nothing, the other legs do     -> we lost a fill
  * ``openalgo_stale``   - broker and we agree, OpenAlgo does not -> OpenAlgo is stale
  * ``broker_only``      - only the broker has it                 -> a manual broker trade
  * ``we_only``          - only we have it                        -> a phantom position
  * ``partial_split``    - two agree, one is between              -> a missed partial fill
  * ``all_agree``        - no action

FAIL-CLOSED RULES
-----------------
- A leg that cannot be read is ``unavailable``, and the whole run's verdict is
  ``blocked``. An entry must not be approved on a comparison that did not happen.
- A symbol present in ANY leg is reported. Silence is not agreement: a symbol our
  ledger has never heard of but the broker holds is the most dangerous case there
  is, and it would be invisible if only common symbols were compared.

This module is read-only. It never places, cancels or modifies anything.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

__all__ = ["LEGS", "reconcile", "reconcile_report"]

#: The three legs, in report order.
LEGS = ("ledger", "openalgo", "broker")


def _clean(positions: Optional[Mapping[str, Any]]) -> Dict[str, int]:
    """Normalise a position map. Non-integer or zero quantities are dropped."""
    out: Dict[str, int] = {}
    for symbol, qty in (positions or {}).items():
        try:
            value = int(qty)
        except (TypeError, ValueError):
            continue
        if value:
            out[str(symbol).strip().upper()] = value
    return out


def _classify(values: Dict[str, Optional[int]]) -> str:
    """Name the disagreement pattern for one symbol."""
    known = {leg: v for leg, v in values.items() if v is not None}
    if len(set(known.values())) <= 1:
        return "all_agree"

    ledger = values.get("ledger") or 0
    openalgo = values.get("openalgo")
    broker = values.get("broker") or 0

    if ledger == 0 and (openalgo or broker):
        return "ours_missing"
    if ledger != 0 and not openalgo and not broker:
        return "we_only"
    if ledger == 0 and not broker and openalgo:
        return "openalgo_only"
    if ledger == broker and openalgo is not None and openalgo != broker:
        return "openalgo_stale"
    if ledger == 0 and broker and not openalgo:
        return "broker_only"

    # One leg sits between two that agree: the signature of a missed partial fill.
    trio = sorted(set(known.values()))
    if len(trio) == 3:
        return "partial_split"
    return "differs"


def reconcile(ledger_positions: Optional[Mapping[str, Any]],
              openalgo_positions: Optional[Mapping[str, Any]],
              broker_positions: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Compare the three position maps.

    Returns::

        {"verdict": "match"|"mismatch"|"blocked",
         "legs": {leg: "ok"|"unavailable"},
         "symbols": {SYMBOL: {"ledger": int|None, "openalgo": int|None,
                              "broker": int|None, "cause": str}},
         "mismatch_count": int, "detail": str}

    A leg passed as None is ``unavailable``. If any leg is unavailable the verdict
    is ``blocked``: a partial comparison cannot clear an entry.
    """
    raw = {"ledger": ledger_positions, "openalgo": openalgo_positions,
           "broker": broker_positions}
    legs = {leg: ("unavailable" if raw[leg] is None else "ok") for leg in LEGS}
    missing_legs = [leg for leg, state in legs.items() if state == "unavailable"]

    books = {leg: _clean(raw[leg]) for leg in LEGS}
    symbols = set()
    for book in books.values():
        symbols.update(book)

    findings: Dict[str, Dict[str, Any]] = {}
    for symbol in sorted(symbols):
        values = {
            "ledger": books["ledger"].get(symbol, 0) if legs["ledger"] == "ok" or symbol in books["ledger"] else None,
            "openalgo": books["openalgo"].get(symbol, 0) if legs["openalgo"] == "ok" or symbol in books["openalgo"] else None,
            "broker": books["broker"].get(symbol, 0) if legs["broker"] == "ok" or symbol in books["broker"] else None,
        }
        cause = _classify(values)
        if cause != "all_agree":
            findings[symbol] = {**values, "cause": cause}

    if missing_legs:
        return {
            "verdict": "blocked",
            "legs": legs,
            "symbols": findings,
            "mismatch_count": len(findings),
            "detail": f"leg(s) unavailable: {', '.join(missing_legs)}; comparison incomplete",
        }

    verdict = "match" if not findings else "mismatch"
    return {
        "verdict": verdict,
        "legs": legs,
        "symbols": findings,
        "mismatch_count": len(findings),
        "detail": ("all three legs agree" if verdict == "match"
                   else f"{len(findings)} symbol(s) disagree across the legs"),
    }


def reconcile_report(result: Dict[str, Any], *, audit=None, alerts=None) -> Dict[str, Any]:
    """Record a reconciliation result and raise an alert when it is not a match.

    ``blocked`` is a WARNING (we could not look); ``mismatch`` is an ERROR (we
    looked and found a disagreement). Both are audited, because a recon run that
    leaves no trace is indistinguishable from one that never ran.
    """
    verdict = str(result.get("verdict") or "blocked")
    if audit is not None:
        try:
            audit.append("recon3", result)
        except Exception as exc:  # noqa: BLE001 - recording must not abort the check
            logger.warning("recon audit write failed: %r", exc)

    if alerts is not None and verdict != "match":
        level = "WARNING" if verdict == "blocked" else "ERROR"
        try:
            alerts.emit(level, "recon3",
                        f"three-way recon {verdict}: {result.get('detail', '')}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("recon alert failed: %r", exc)

    return result
