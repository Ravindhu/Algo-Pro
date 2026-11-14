"""Pipeline orchestrator wrapper: the master pipeline in DRY RUN only, DELEGATED to Blue_engine.

Flow stage: Pipeline orchestrator
Status: PHASE 5 IMPLEMENTED (wrap; dry run forced ON; no SDK client ever passed)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/core/pipeline.py (run_pipeline)

Blue's pipeline places orders only when ``dry_run`` is False AND an SDK client is in
its context. This wrapper makes both impossible:
- ``dry_run`` is forced to True, whatever the config says.
- Every context key that looks like a client, SDK or broker handle is removed.
The legacy orchestrator (core/orchestrator.py, with its order stage) is NOT wrapped.
The supervisor's restart and broker gates are NOT wrapped either.

FAIL-CLOSED RULES
-----------------
- Blue unavailable or the run raises: ``ok=False``.
- Never raises.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["run_dry"]

_CLIENT_HINTS = ("client", "sdk", "openalgo", "broker", "api_key")


def run_dry(config: Optional[Dict[str, Any]] = None,
            context: Optional[Dict[str, Any]] = None,
            log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Run the pipeline with ``dry_run`` forced on and no client. Never raises."""
    failed = {"ok": False, "dry_run": True, "order_path": "none", "stripped_keys": []}
    try:
        run_pipeline = load("core.pipeline").run_pipeline
    except BlueEngineUnavailable as exc:
        return {**failed, "reason": f"Blue pipeline unavailable: {exc}"}
    cfg = dict(config or {})
    cfg["dry_run"] = True
    log = log or (lambda _msg: None)
    ctx: Dict[str, Any] = {}
    stripped: List[str] = []
    for key, value in dict(context or {}).items():
        if any(hint in str(key).lower() for hint in _CLIENT_HINTS):
            stripped.append(str(key))
            continue
        ctx[key] = value
    try:
        result = run_pipeline(cfg, ctx, log)
    except Exception as exc:  # noqa: BLE001
        return {**failed, "stripped_keys": stripped, "reason": f"pipeline failed: {exc!r}"}
    out = dict(result) if isinstance(result, dict) else {"result": result}
    out.update({"ok": True, "dry_run": True, "order_path": "none", "stripped_keys": stripped})
    return out
