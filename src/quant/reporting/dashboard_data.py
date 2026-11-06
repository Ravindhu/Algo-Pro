"""Read-only data for the FastAPI dashboard: positions, FIFO P&L, safety events, AI insight.

Owner rule (AGENTS rule 8): P&L is shown. EV, MAE/MFE and rule compliance are NOT in this payload.
Nothing here sends orders. Inputs are plain records read from the ledger, audit log and kill switch.

Flow stage: Dashboard
Status: PHASE 3 IMPLEMENTED (paper data only; no live feed)
Existing code to reuse or wrap (do not copy): reporting/dashboard.py (build_payload, win rate, profit factor)
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from quant.reporting.dashboard import build_payload

SEVERITY = {"kill_switch": "CRITICAL", "kill_close": "CRITICAL", "safe_mode": "WARNING", "override": "WARNING",
            "limits_changed": "WARNING"}


def fifo_pnl(fills: Iterable[Mapping]) -> Dict[str, object]:
    """Realized P&L per symbol with first-in-first-out matching.

    fills: dicts with symbol, signed_qty (+buy / -sell), price, in time order.
    Returns closed trades (pnl, ret) and open lots (qty, avg_price) per symbol.
    """
    lots: Dict[str, List[list]] = defaultdict(list)  # symbol -> [[signed_qty, price], ...]
    closed: List[dict] = []
    for f in fills:
        sym, q, px = f["symbol"], int(f["signed_qty"]), float(f["price"])
        book = lots[sym]
        while q != 0 and book and (book[0][0] > 0) != (q > 0):
            lot_q, lot_px = book[0]
            m = min(abs(q), abs(lot_q))
            sign = 1 if lot_q > 0 else -1  # long lot closed by a sell: gain when price rises
            pnl = sign * (px - lot_px) * m
            closed.append({"symbol": sym, "qty": m, "entry": lot_px, "exit": px,
                           "pnl": pnl, "ret": pnl / (lot_px * m) if lot_px else 0.0})
            book[0][0] = lot_q - sign * m
            q += sign * m  # q moves toward zero by the matched amount
            if book[0][0] == 0:
                book.pop(0)
        if q != 0:
            book.append([q, px])
    open_pos = {
        s: {"qty": sum(l[0] for l in book),
            "avg_price": sum(l[0] * l[1] for l in book) / sum(l[0] for l in book)}
        for s, book in lots.items() if book
    }
    return {"closed": closed, "open": open_pos}


def risk_utilisation(open_pos: Mapping[str, Mapping], max_open_exposure: Optional[float]) -> float:
    """Open exposure (abs qty x avg price) as a share of max_open_exposure. 0.0 when the limit is unset."""
    if not max_open_exposure:
        return 0.0
    exposure = sum(abs(p["qty"]) * p["avg_price"] for p in open_pos.values())
    return exposure / max_open_exposure


def unrealized(open_pos: Mapping[str, Mapping], last_prices: Mapping[str, float]) -> Dict[str, Optional[float]]:
    """Unrealized P&L per open symbol. None when no price is known (never guessed)."""
    out: Dict[str, Optional[float]] = {}
    for s, p in open_pos.items():
        px = last_prices.get(s)
        out[s] = None if px is None else (px - p["avg_price"]) * p["qty"]
    return out


def safety_events(audit_records: Sequence[Mapping], kill: Mapping) -> List[dict]:
    """Newest-first list of safety events. Kill switch first when active."""
    events: List[dict] = []
    for rec in audit_records:
        kind = rec.get("kind", "")
        if kind in SEVERITY:
            p = rec.get("payload", {})
            msg = p.get("reason") or p.get("to") or kind
            events.append({"severity": SEVERITY[kind], "kind": kind, "message": str(msg), "ts": rec.get("ts")})
    events.reverse()  # newest first
    if kill.get("active"):
        events.insert(0, {"severity": "CRITICAL", "kind": "kill_switch",
                          "message": f"Kill switch active: {kill.get('reason', '')}", "ts": None})
    return events


def current_mode(audit_records: Sequence[Mapping]) -> str:
    """Last safe-mode change in the audit log; 'normal' when none."""
    for rec in reversed(audit_records):
        if rec.get("kind") == "safe_mode":
            return str(rec.get("payload", {}).get("to", "normal"))
    return "normal"


def ai_insight(overview: Mapping) -> List[str]:
    """Rule-based sentences from the numbers. Advice only: no LLM, no trade path."""
    lines: List[str] = []
    if overview["kill_switch"]["active"]:
        lines.append("Entries blocked and open positions closed by the kill switch.")
    if overview["unset_limits"]:
        lines.append(f"Limits not set: {', '.join(overview['unset_limits'])}. Entries fail closed.")
    pf = overview["performance"]["profit_factor"]
    if pf is not None and pf < 1.0:
        lines.append("Profit factor is below 1: losses exceed gains so far. Review before more trades.")
    if overview["performance"]["trades"] == 0:
        lines.append("No closed trades yet. Nothing to judge.")
    if not lines:
        lines.append("No rule alerts. Paper data only.")
    return lines


def build_overview(
    fills: Sequence[Mapping],
    audit_records: Sequence[Mapping],
    kill: Mapping,
    limits: Mapping[str, object],
    last_prices: Mapping[str, float],
    risk_used: Optional[float] = None,
) -> Dict[str, object]:
    pnl = fifo_pnl(fills)
    if risk_used is None:
        risk_used = risk_utilisation(pnl["open"], limits.get("max_open_exposure"))
    perf = build_payload(
        [{"ret": t["ret"], "pnl": t["pnl"]} for t in pnl["closed"]], risk_used
    )
    upnl = unrealized(pnl["open"], last_prices)
    unset = [k for k, v in limits.items() if k != "blocked" and v is None]
    overview = {
        "mode": current_mode(audit_records),
        "kill_switch": {"active": bool(kill.get("active")), "reason": kill.get("reason", "")},
        "limits": dict(limits),
        "unset_limits": unset,
        "positions": [
            {"symbol": s, "qty": p["qty"], "avg_price": round(p["avg_price"], 2),
             "unrealized_pnl": None if upnl[s] is None else round(upnl[s], 2)}
            for s, p in sorted(pnl["open"].items())
        ],
        "performance": {
            "trades": perf["trades"],
            "realized_pnl": round(perf["total_pnl"], 2),
            "unrealized_pnl": round(sum(v for v in upnl.values() if v is not None), 2),
            "win_rate": perf["win_rate"],
            "profit_factor": perf["profit_factor"],
            "risk_utilisation": risk_used,
        },
        "events": safety_events(audit_records, kill)[:20],
    }
    overview["insight"] = ai_insight(overview)
    return overview
