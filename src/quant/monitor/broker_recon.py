"""Startup broker reconcile for the BUY book. Entries stay blocked until broker and bot agree.

Flow stage: Ledger and recon (startup gate)
Status: PHASE 2 IMPLEMENTED (read-only; no order call; 2026-11-08)

How it fits:
- Runs once at startup, AFTER open positions are restored from the state file, so the bot's view is
  complete before it compares.
- Reads the broker's position book through the read-only OpenAlgo client (no place/cancel method exists).
- Compares net quantity per symbol with our open long positions, using the existing recon job
  (monitor/recon_job.run_recon). Our book is long-only, so any broker short is a mismatch.
- Result: "match" allows entries. "mismatch" or "unavailable" (read failed or unreadable) blocks
  entries. Exits are NOT blocked: risk-reducing exits must still go out.

Assumption to verify against live OpenAlgo output: the positionbook body is
  {"status": "success", "data": [{"symbol": ..., "exchange": ..., "netqty": ...}, ...]}.
An unexpected shape raises, and the result is "unavailable" (fail closed).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional

from quant.monitor.recon_job import run_recon

EXCHANGE = "NSE"


def parse_positionbook(body: Any, exchange: str = EXCHANGE) -> Dict[str, int]:
    """Net quantity per symbol for one exchange. Raises ValueError on an unexpected body."""
    if not isinstance(body, dict) or body.get("status") != "success":
        raise ValueError(f"positionbook not successful: {body!r:.200}")
    rows = body.get("data")
    if not isinstance(rows, list):
        raise ValueError("positionbook data is not a list")
    out: Dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict) or "symbol" not in row:
            raise ValueError(f"positionbook row without symbol: {row!r:.200}")
        if str(row.get("exchange", exchange)) != exchange:
            continue
        qty = int(float(row.get("netqty", 0)))
        out[str(row["symbol"])] = out.get(str(row["symbol"]), 0) + qty
    return out


def make_positionbook_reader(readonly: Any, api_key: str, exchange: str = EXCHANGE) -> Callable[[], Dict[str, int]]:
    """Broker reader for startup_reconcile. Uses only the read-only client's positionbook call."""
    def fetch() -> Dict[str, int]:
        return parse_positionbook(readonly.positionbook(api_key), exchange)
    return fetch


class PositionsView:
    """Our open long quantity per symbol, shaped like the ledger that recon_job.run_recon expects."""

    def __init__(self, positions: Any) -> None:
        self._positions = positions

    def positions(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for pos in self._positions.open_positions().values():
            out[pos.symbol] = out.get(pos.symbol, 0) + int(pos.qty)
        return out

    def recon(self, broker_positions: Mapping[str, int]) -> Dict[str, tuple]:
        ours = self.positions()
        symbols = set(ours) | set(broker_positions)
        return {s: (ours.get(s, 0), int(broker_positions.get(s, 0)))
                for s in symbols if ours.get(s, 0) != int(broker_positions.get(s, 0))}


@dataclass
class ReconGate:
    status: str                       # "match" | "mismatch" | "unavailable" | "skipped"
    mismatches: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def entries_allowed(self) -> bool:
        return self.status in ("match", "skipped")


def startup_reconcile(positions: Any, fetch_broker_positions: Optional[Callable[[], Dict[str, int]]],
                      *, audit: Any = None, require_broker: bool = False) -> ReconGate:
    """Compare broker and bot positions once. Call after the state file is restored.

    fetch_broker_positions None: with require_broker (live) this raises, because a live start with no broker
    read must not run. Otherwise (paper, no broker) the result is "skipped" and entries are allowed."""
    if fetch_broker_positions is None:
        if require_broker:
            raise ValueError("live startup needs a broker position reader (fail closed)")
        if audit is not None:
            audit.append("recon", {"status": "skipped", "reason": "paper: no broker to compare with"})
        return ReconGate(status="skipped")

    result = run_recon(PositionsView(positions), fetch_broker_positions, audit=audit)
    return ReconGate(status=result["status"], mismatches=result.get("mismatches", {}),
                     error=result.get("error"))
