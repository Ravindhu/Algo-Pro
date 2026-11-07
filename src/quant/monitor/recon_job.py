"""Reconciliation job: ledger positions vs broker positions. Every run is logged.

Flow stage: Ledger and recon
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): none (wraps monitor/ledger.py recon, flow item 11)

The broker reader is passed in. It must be read-only. A failed read is logged as unavailable,
and no positions are changed.
"""
from __future__ import annotations

from typing import Callable, Dict


def run_recon(ledger, fetch_broker_positions: Callable[[], Dict[str, int]], audit=None, alerts=None) -> dict:
    try:
        broker = fetch_broker_positions()
    except Exception as exc:
        result = {"status": "unavailable", "mismatches": {}, "error": str(exc)}
        level = "WARNING"
    else:
        mismatches = ledger.recon(broker)
        result = {
            "status": "match" if not mismatches else "mismatch",
            "mismatches": {sym: list(pair) for sym, pair in mismatches.items()},
        }
        level = "INFO" if not mismatches else "ERROR"
    if audit is not None:
        audit.append("recon", result)
    if alerts is not None:
        alerts.emit(level, "recon", f"status={result['status']} mismatches={len(result['mismatches'])}")
    return result
