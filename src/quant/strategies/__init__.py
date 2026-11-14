"""Strategy registry: named strategies resolved through Blue_engine, never re-implemented.

Flow stage: Signal
Status: PHASE 5 IMPLEMENTED (registry + adapters; strategy bodies stay in Blue)
Existing code to reuse or wrap (do not copy): src/quant/blue_engine/alpha/registry.py
(list_strategies, strategy_module), src/quant/blue_engine/alpha/signals (``available``, ``get``),
src/quant/blue_engine/optimizer/strategies.py (resolve_strategy, built-in strategy functions)

WHAT A STRATEGY IS HERE
-----------------------
Three callables are needed by three different stages, and they are NOT the same
shape. Keeping them apart is what stops one adapter from being quietly used in
the wrong place:

``signal_fn``        ``(params, ctx, bars=None) -> {returns: [...]}``
                     The optimiser and the single-symbol backtest contract. A
                     resolved name maps to a FUNCTION.
``live_signal_class``
                     ``BaseSignal`` subclass with ``detect() -> intent|None``.
                     The live runner instantiates it with a client. A resolved
                     name maps to a CLASS.
``list_strategies``  the read-only registry view: name, label, module, kind.

It is a mistake to pass a live signal class into the optimiser: it needs a
broker client at construction. ``as_optimizer_strategy`` refuses it with a clear
error rather than producing a study that scores a strategy which cannot trade.

NO ORDER PATH
-------------
Nothing in this package imports an execution client. A strategy returns an
intent dict; only the risk gate may act on one.

LONG-ONLY (owner, 2026-11-09)
-----------------------------
Investment algo: BUY and ADD, never short. A strategy may only ever return
``action`` BUY (or no action). ``guard_intent`` is the check every caller must
run before an intent leaves this package; it refuses SELL/SHORT entries with a
reason instead of flipping or dropping the order silently.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from quant.core.blue_bridge import BlueEngineUnavailable, load

__all__ = ["StrategyUnavailable", "StrategyRef", "list_strategies",
           "resolve_signal_fn", "resolve_live_class", "available_signal_fns",
           "guard_intent"]


class StrategyUnavailable(RuntimeError):
    """Blue's strategy registry cannot be loaded."""


@dataclass(frozen=True)
class StrategyRef:
    """One registry row, copied (not aliased) so a caller cannot mutate the registry."""

    name: str
    label: str = ""
    module: str = ""
    kind: str = ""              # "live" (alpha/Statergy) or "signal" (alpha/signals)
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "label": self.label, "module": self.module,
                "kind": self.kind, "description": self.description}


def list_strategies() -> List[StrategyRef]:
    """Every strategy the alpha engine has registered. Never raises; empty on failure."""
    try:
        rows = load("alpha.registry").list_strategies()
    except BlueEngineUnavailable:
        return []
    out: List[StrategyRef] = []
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        out.append(StrategyRef(
            name=str(row["name"]), label=str(row.get("label") or ""),
            module=str(row.get("module") or ""), kind=str(row.get("kind") or ""),
            description=str(row.get("description") or "")))
    return sorted(out, key=lambda r: r.name)


def resolve_live_class(name: str):
    """The ``BaseSignal`` subclass registered as ``name``, or ``None``.

    ``None`` is the contract: a missing strategy is a normal state while a desk
    is being configured, not an exception.
    """
    if not name:
        return None
    try:
        return load("alpha.signals").get(str(name))
    except BlueEngineUnavailable:
        return None


def available_signal_fns() -> Dict[str, str]:
    """Name -> dotted path for the optimiser-contract strategies Blue ships."""
    try:
        return dict(load("optimizer.strategies").available_builtin_strategies())
    except BlueEngineUnavailable:
        return {}


def resolve_signal_fn(spec: Any) -> Tuple[Optional[Callable[..., Dict[str, Any]]], Optional[str]]:
    """Resolve an optimiser-contract strategy spec to a callable.

    Returns ``(fn, error)`` and never raises, mirroring Blue's own contract so a
    typo prints one line instead of a traceback.

    A ``BaseSignal`` CLASS is refused here with an explanation: it needs a broker
    client and returns intents, so it cannot be scored on returns.
    """
    if isinstance(spec, type):
        return None, (f"{getattr(spec, '__name__', spec)} is a live signal class "
                      "(needs a client, returns intents); use a signal function")
    try:
        return load("optimizer.strategies").resolve_strategy(spec)
    except BlueEngineUnavailable as exc:
        return None, f"Blue optimiser strategies unavailable: {exc}"


def guard_intent(intent: Optional[Dict[str, Any]], *,
                 exit_leg: bool = False) -> Tuple[bool, str]:
    """Long-only check on a signal intent. ``(allowed, reason)``. Never raises.

    Rules:
    - No intent, or an empty one, is allowed: no action is a valid answer.
    - ``action`` BUY or ADD is allowed.
    - ``action`` SELL or SHORT is allowed ONLY with ``exit_leg=True``, which
      declares it closes an existing long. Otherwise it is a short entry and is
      refused - the investment book never opens a short.
    - An action that is missing, blank or unrecognised is REFUSED, because an
      order whose direction cannot be read must not be sent.
    """
    if intent is None:
        return True, "no action"
    if not isinstance(intent, dict):
        return False, "intent is not a dict"
    raw = intent.get("action")
    if raw is None or not str(raw).strip():
        return True, "no action"
    action = str(raw).strip().upper()
    if action in ("BUY", "ADD"):
        return True, ""
    if action in ("SELL", "SHORT"):
        if exit_leg:
            return True, "exit leg: closing an existing long"
        return False, (f"long-only: {action} refused; a SELL is only an exit and "
                       "must be declared with exit_leg=True")
    return False, f"long-only: unrecognised action {action!r} refused"
