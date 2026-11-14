"""Infrastructure wrapper: local health gate (ports, disk, dashboard), DELEGATED to Blue_engine.

Flow stage: Infrastructure engine
Status: PHASE 5 IMPLEMENTED (wrap; local probes only; the broker is never called from here)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/infrastructure/infrastructure_engine.py

The Blue engine can also call the broker (funds, order book) when it is given an SDK
client. This wrapper NEVER passes a client. So the broker session is not checked here.
Use ``execution/broker_auth_status.py`` and the feed monitor for that. ``ready`` says
the local parts are up. It does not say the broker is logged in.

FAIL-CLOSED RULES
-----------------
- Blue unavailable or a probe raises: ``ready=False``.
- A closed OpenAlgo port, an unhealthy disk, or a missing dashboard: ``ready=False``.
- Never raises.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["health_gate"]


def health_gate(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return ``{ready, broker_checked, sections, reason}``. Never raises."""
    failed = {"ready": False, "broker_checked": False, "sections": {}}
    try:
        cls = load("infrastructure.infrastructure_engine").InfrastructureEngine
    except BlueEngineUnavailable as exc:
        return {**failed, "reason": f"Blue infrastructure engine unavailable: {exc}"}
    try:
        result = cls(dict(config or {})).run({})  # no "client" in context: broker never called
    except Exception as exc:  # noqa: BLE001
        return {**failed, "reason": f"infrastructure check failed: {exc!r}"}
    data = getattr(result, "data", None) or {}
    # Blue nests the per-check results under data["checks"]; "ready_to_trade" needs the
    # broker, which this wrapper never checks, so it is deliberately not used.
    sections = {k: v for k, v in dict(data.get("checks") or {}).items() if isinstance(v, dict)}
    needed = ("openalgo", "dashboard", "servers")
    present = all(name in sections for name in needed)
    local_ok = present and all(bool(sections[name].get("ok")) for name in needed)
    ready = bool(local_ok and getattr(result, "ok", False))
    return {
        "ready": ready,
        "broker_checked": False,
        "sections": sections,
        "reason": "" if ready else "a local check is failing or missing: entries blocked",
    }
