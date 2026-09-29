"""
risk_context.py - live P&L / exposure feed for the pre-trade RiskGate (WIRE_PNL).

Why this exists
---------------
The RiskGate (core/risk_gate.py) evaluates ``max_daily_loss`` and
``max_open_exposure`` from the ``day_pnl`` and ``open_exposure`` values its
caller passes in. Until now the runner only took those from the JSON config —
hand-typed, quickly stale, and often absent, in which case those two checks
silently skipped (the gate is fail-open on unconfigured limits by design).
That is a capital-protection gap: the daily-loss circuit breaker only worked
if the operator typed the number that morning.

This module closes it: it asks the BROKER for the truth via the OpenAlgo SDK
(``client.positionbook()``), derives

* ``day_pnl``      — the sum of per-position P&L the broker reports
  (realised + unrealised for the day, per the broker's semantics), and
* ``open_exposure`` — the absolute notional of every non-zero position
  (signed quantity x reference price),

and returns them for the RiskGate. An explicitly provided config value still
wins (operator override); the broker feed is the FALLBACK so the checks can
never silently skip again.

Design rules
------------
* FAIL-SOFT, never raises: any SDK/parse error degrades to ``{"ok": False}``
  and the caller falls back to config-only behaviour with a loud warning.
* SDK-only: public ``client.positionbook()`` — no OpenAlgo internals, no
  direct REST, no DB.
* Field-tolerant parsing: brokers name the same field differently
  (``pnl`` / ``pandl`` / ``day_pnl``, ``netqty`` / ``net_qty`` /
  ``quantity``, ``avgprc`` / ``avg_price`` / ``buyavgprice``). We probe
  synonyms rather than assuming one schema.
* Data classification: the numbers here are LIVE broker data. Nothing in
  this module should ever be presented as backtested or simulated.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["fetch_risk_context", "parse_positionbook"]

# Field synonyms, probed in order of preference. Per-position P&L.
_PNL_KEYS = ("pnl", "day_pnl", "pandl", "realized_pnl", "unrealized_pnl",
             "realised", "unrealized")
# Signed net quantity of the open position.
_QTY_KEYS = ("netqty", "net_qty", "net_quantity", "quantity", "qty")
# Price used for notional: average price first, then last traded price.
_PRICE_KEYS = ("avgprc", "avg_price", "average_price", "buyavgprice",
               "ltp", "last_price", "lastprice")


def _num(value: Any) -> Optional[float]:
    """Parse a finite float, else None (mirrors risk_gate._safe_float)."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _first_num(row: Dict[str, Any], keys) -> Optional[float]:
    for key in keys:
        n = _num(row.get(key))
        if n is not None:
            return n
    return None


def parse_positionbook(resp: Any) -> Dict[str, Any]:
    """Derive day P&L and open exposure from a positionbook response.

    Args:
        resp: the dict returned by ``client.positionbook()`` (or anything;
            malformed input yields an ``ok=False`` report, never a raise).

    Returns:
        ``{"ok": bool, "day_pnl": float|None, "open_exposure": float|None,
        "n_positions": int, "errors": [str]}``
    """
    out: Dict[str, Any] = {"ok": False, "day_pnl": None,
                           "open_exposure": None, "n_positions": 0,
                           "errors": []}
    try:
        if not isinstance(resp, dict) or resp.get("status") != "success":
            out["errors"].append(
                f"positionbook not successful: {resp!r:.200}" if isinstance(
                    resp, dict) else f"positionbook not a dict: {type(resp)!r}")
            return out
        data = resp.get("data")
        rows: List[Dict[str, Any]] = []
        if isinstance(data, list):
            rows = [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict):
            # Some brokers wrap the list one level down (e.g. "positions").
            for key in ("positions", "book", "rows"):
                if isinstance(data.get(key), list):
                    rows = [r for r in data[key] if isinstance(r, dict)]
                    break
        if not rows:
            out["errors"].append("positionbook returned no position rows")
            return out

        day_pnl = 0.0
        any_pnl = False
        exposure = 0.0
        open_positions = 0
        for row in rows:
            pnl = _first_num(row, _PNL_KEYS)
            if pnl is not None:
                day_pnl += pnl
                any_pnl = True
            qty = _first_num(row, _QTY_KEYS)
            price = _first_num(row, _PRICE_KEYS)
            if qty is not None and abs(qty) > 0 and price is not None:
                exposure += abs(qty * price)
                open_positions += 1
        out.update(ok=True, day_pnl=day_pnl if any_pnl else None,
                   open_exposure=exposure, n_positions=open_positions)
        return out
    except Exception as exc:  # noqa: BLE001 - fail-soft by contract
        out["errors"].append(repr(exc))
        return out


def fetch_risk_context(client: Any, config: Optional[Dict[str, Any]] = None,
                       ) -> Dict[str, Any]:
    """Fetch live risk inputs for the RiskGate, config values taking precedence.

    Resolution order (highest wins):
        1. ``config["day_pnl"]`` / ``config["open_exposure"]`` — explicit
           operator override in the master JSON;
        2. the broker's positionbook via the OpenAlgo SDK;
        3. ``None`` — the corresponding RiskGate check skips, with a warning
           recorded so the skip is visible, never silent.

    Args:
        client: an OpenAlgo SDK client (or None / stub with no
            ``positionbook`` — handled as degraded).
        config: the raw master config (its ``risk`` block is read).

    Returns:
        ``{"day_pnl", "open_exposure", "source", "ok", "warnings"}``
        where ``source`` is "config", "broker", or "none".
    """
    cfg = config if isinstance(config, dict) else {}
    risk_block = cfg.get("risk") if isinstance(cfg.get("risk"), dict) else {}
    warnings: List[str] = []

    day_pnl = _num(risk_block.get("day_pnl"))
    open_exposure = _num(risk_block.get("open_exposure"))
    if day_pnl is not None or open_exposure is not None:
        return {"day_pnl": day_pnl, "open_exposure": open_exposure,
                "source": "config", "ok": True, "warnings": warnings}

    if client is None or not callable(getattr(client, "positionbook", None)):
        warnings.append(
            "no broker positionbook available; day_pnl / open_exposure "
            "checks will be SKIPPED (fail-open) — set them in the risk "
            "config or fix the OpenAlgo connection")
        logger.warning("risk_context: %s", warnings[-1])
        return {"day_pnl": None, "open_exposure": None,
                "source": "none", "ok": False, "warnings": warnings}

    try:
        resp = client.positionbook()
    except Exception as exc:  # noqa: BLE001 - degrade, never abort the run
        warnings.append(f"positionbook call failed: {exc!r}")
        logger.warning("risk_context: %s", warnings[-1])
        return {"day_pnl": None, "open_exposure": None,
                "source": "none", "ok": False, "warnings": warnings}

    parsed = parse_positionbook(resp)
    if not parsed["ok"]:
        warnings.extend(
            f"positionbook unusable: {e}" for e in parsed["errors"])
        logger.warning("risk_context: %s", warnings[-1])
        return {"day_pnl": None, "open_exposure": None,
                "source": "none", "ok": False, "warnings": warnings}

    if day_pnl is None and parsed["day_pnl"] is None:
        warnings.append(
            "broker reported positions but no P&L field; max_daily_loss "
            "check will skip")
    return {"day_pnl": parsed["day_pnl"],
            "open_exposure": parsed["open_exposure"],
            "source": "broker", "ok": True, "warnings": warnings}
