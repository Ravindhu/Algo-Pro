"""Live status for each node of the v5 flow popup. Pure function, no I/O.

Input: the overview dict built by dashboard/app.py load_state.
Output: {node label: {"status": "warn" | "err", "message": str}}. Only nodes that need attention are listed;
the page paints every other node as ok. "err" blinks red, "warn" is amber.

Rules use CURRENT state only (kill switch, mode, loop, feed, calendar, limits). Old audit events are not
used, so a kill switch reset clears the red at once. Labels must match data-label values from
quant_system_flow_v5.svg (tests/unit/test_flow_status.py checks this).

Flow stage: Dashboard
Status: PHASE 3 IMPLEMENTED (paper data only)
"""
from __future__ import annotations

from typing import Dict, Mapping

WARN, ERR = "warn", "err"
RANK = {WARN: 1, ERR: 2}

# unset limit -> node that depends on it
LIMIT_NODES = {
    "max_order_quantity": "Pre-trade checks",
    "max_order_notional": "Pre-trade checks",
    "max_adv_pct": "Liquidity cap",
    "max_daily_loss": "Loss budget breaker",
    "max_strategy_loss": "Loss budget breaker",
    "max_open_exposure": "Portfolio risk",
    "max_symbols_per_portfolio": "Risk gate",
}


def _add(board: Dict[str, Dict[str, str]], label: str, status: str, message: str) -> None:
    """Keep the worst status per node; join messages that share the same status."""
    cur = board.get(label)
    if cur is None or RANK[status] > RANK[cur["status"]]:
        board[label] = {"status": status, "message": message}
    elif RANK[status] == RANK[cur["status"]]:
        cur["message"] = cur["message"] + "; " + message


def node_statuses(overview: Mapping[str, object]) -> Dict[str, Dict[str, str]]:
    board: Dict[str, Dict[str, str]] = {}

    kill = overview.get("kill_switch") or {}
    if kill.get("active"):
        _add(board, "Kill switch", ERR, "Kill switch on: " + str(kill.get("reason") or "no reason given"))

    mode = str(overview.get("mode", "normal"))
    if mode != "normal":
        status = ERR if "halt" in mode else WARN
        _add(board, "Safe mode controller", status, "Safe mode: " + mode)

    loop = overview.get("loop") or {}
    if loop.get("last_error"):
        _add(board, "Paper trading stage", ERR, "Loop error: " + str(loop["last_error"]))

    market = overview.get("market") or {}
    if market and not market.get("verified", False):
        _add(board, "Trading calendar", WARN, "Holiday calendar not verified")

    feed = overview.get("feed") or {}
    if feed.get("status") != "connected":
        note = str(feed.get("note") or "feed not connected")
        _add(board, "Broker feeds", WARN, note)
        _add(board, "Feed health monitor", WARN, note)

    for name in overview.get("unset_limits") or []:
        label = LIMIT_NODES.get(name)
        if label:
            _add(board, label, WARN, name + " not set: entries fail closed")

    return board
