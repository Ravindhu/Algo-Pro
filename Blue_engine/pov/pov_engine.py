"""
pov.py - the POV (Percentage-of-Volume) engine adapter.

This is the *execution* stage of the macro pipeline. It does not re-implement
slicing; it delegates to the existing ``blue_engine.pov.slicers.pov.POVSlicer``
(the proven, guarded, ledger-backed executor) and layers execution-cost
analysis on top.

Two responsibilities:

  1. slice      - build a POVSlicer config from the upstream Alpha/Portfolio
                  decisions and run it against the OpenAlgo SDK client. The
                  client is passed through ``context['client']``; when it is
                  absent the engine reports ``skipped`` rather than touching
                  the network.
  2. cost       - track execution cost (TCA): implementation shortfall vs the
                  arrival price, participation rate, and reversion after the
                  trade. The numbers come from the ledger/benchmark helpers,
                  so they are consistent with the live slicer.

This module keeps the engines/base contract (returns an EngineResult) while
honouring the locked architecture: SDK-only, no OpenAlgo-internal imports.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ..core.base import BaseEngine, EngineResult, EngineStage
from ..core.results import clamp, safe_float


def _build_pov_config(base: Dict[str, Any], symbol: str, exchange: str,
                      action: str, quantity: int,
                      portfolio: Dict[str, Any]) -> Dict[str, Any]:
    """Map the upstream decisions onto a POVSlicer config dict."""
    cfg = dict(base)
    cfg["symbol"] = symbol
    cfg["exchange"] = exchange
    cfg["action"] = action
    cfg["quantity"] = int(quantity)

    # Scale participation down when the portfolio says we are de-risking.
    gross = safe_float(portfolio.get("gross_exposure_multiplier")) if portfolio else None
    if gross is not None and gross < 0.75:
        base_pct = safe_float(cfg.get("participation_pct")) or 10.0
        cfg["participation_pct"] = max(1.0, base_pct * gross)
    return cfg


class PovEngine(BaseEngine):
    """Delegate slicing to POVSlicer and report execution cost."""

    name = "pov"
    stage = EngineStage.POV

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 slicer_factory: Optional[Callable[..., Any]] = None,
                 **kwargs: Any):
        super().__init__(config, provider, kwargs.get("log"))
        #: Inject a slicer factory for testing; default resolves lazily so the
        #: engines package imports cleanly even without the SDK installed.
        self._slicer_factory = slicer_factory

    def _get_pov_config(self) -> Dict[str, Any]:
        if isinstance(self.config.get("pov"), dict):
            return self.config["pov"]
        return {k: v for k, v in self.config.items() if k not in ("engine",)}

    def _resolve_slicer(self, client: Any, pov_cfg: Dict[str, Any]):
        if self._slicer_factory is not None:
            return self._slicer_factory(client, pov_cfg)
        # Lazy import keeps the engines package SDK-free at import time.
        from blue_engine.pov.slicers.pov import POVSlicer
        return POVSlicer(client, pov_cfg)

    # ---- 1. slicing ---------------------------------------------------- #
    def _slice(self, context: Dict[str, Any], warnings: List[str]):
        client = context.get("client")
        portfolio = {}
        if isinstance(context.get("portfolio"), dict):
            portfolio = context["portfolio"].get("data", {})

        # Choose the symbol/exchange: explicit order in the context wins,
        # else the first eligible Alpha candidate.
        symbol = context.get("symbol")
        exchange = context.get("exchange")
        action = context.get("action")
        quantity = context.get("quantity")

        alpha = {}
        if isinstance(context.get("alpha"), dict):
            alpha = context["alpha"].get("data", {})
        if not symbol:
            selected = alpha.get("selected") or []
            if selected:
                symbol = selected[0]
        if not exchange:
            exchange = self.config.get("exchange", "NSE")
        if not action:
            signals = alpha.get("signals") or {}
            action = signals.get(symbol, "BUY") if symbol else "BUY"

        if not symbol:
            return {"status": "skipped", "reason": "no symbol selected"}, warnings

        pov_cfg = self._get_pov_config()
        if quantity is None:
            quantity = pov_cfg.get("quantity") or pov_cfg.get("total_quantity") or 0
        quantity = int(quantity or 0)
        if quantity <= 0:
            return {"status": "skipped", "reason": "zero quantity"}, warnings

        cfg = _build_pov_config(pov_cfg, symbol, exchange, str(action),
                                quantity, portfolio)

        if client is None:
            warnings.append("no OpenAlgo client in context; slice not submitted")
            return {
                "status": "dry_run",
                "reason": "no client",
                "config": cfg,
            }, warnings

        try:
            slicer = self._resolve_slicer(client, cfg)
            summary = slicer.execute({
                "action": str(action),
                "quantity": quantity,
                "tag": cfg.get("strategy_label", "POV"),
                "reason": "macro pipeline",
            })
            return {"status": "executed", "summary": summary,
                    "symbol": symbol, "exchange": exchange}, warnings
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"slicer failed: {exc!r}")
            return {"status": "error", "error": repr(exc)}, warnings

    # ---- 2. execution cost -------------------------------------------- #
    @staticmethod
    def _execution_cost(summary: Dict[str, Any]) -> Dict[str, Any]:
        """Derive TCA metrics from a slicer summary.

        The slicer summary carries ``filled``, ``target``, ``child_orders``
        and per-child ``ltp_at_place``. We compute:
          * fill_rate          : filled / target
          * participation      : children are POV-sized; a high child count
                                 relative to filled qty implies over-slicing
          * price_drift_bps    : (last child ltp - first child ltp) / first,
                                 a proxy for implementation shortfall when the
                                 arrival price is the first child's ltp.
        """
        filled = int(summary.get("filled") or 0)
        target = int(summary.get("target") or 0)
        children = summary.get("children") or []
        metrics: Dict[str, Any] = {
            "fill_rate": round(filled / target, 4) if target else None,
            "child_orders": int(summary.get("child_orders") or len(children)),
            "avg_child_qty": None,
            "price_drift_bps": None,
        }
        if children:
            qtys = [int(c.get("attempted") or 0) for c in children]
            if qtys:
                metrics["avg_child_qty"] = round(sum(qtys) / len(qtys), 2)
            ltps = [safe_float(c.get("ltp_at_place")) for c in children]
            ltps = [p for p in ltps if p and p > 0]
            if len(ltps) >= 2:
                drift = (ltps[-1] - ltps[0]) / ltps[0] * 10_000.0
                phase = str(summary.get("phase") or "accumulating")
                # For a BUY accumulation, price rising is adverse slippage.
                metrics["price_drift_bps"] = round(drift, 2)
                metrics["adverse_drift_bps"] = round(
                    drift if phase != "exiting" else -drift, 2)
        return metrics

    # ---- main ---------------------------------------------------------- #
    def _run(self, context: Dict[str, Any]) -> EngineResult:
        warnings: List[str] = []
        sliced, warnings = self._slice(context, warnings)
        cost: Dict[str, Any] = {}
        if sliced.get("status") == "executed":
            cost = self._execution_cost(sliced.get("summary") or {})

        self.log(f"slice status={sliced.get('status')} cost={cost}")
        return EngineResult(
            engine=self.name,
            ok=sliced.get("status") != "error",
            data={"slice": sliced, "execution_cost": cost},
            warnings=warnings,
            error=sliced.get("error") if sliced.get("status") == "error" else None,
        )


__all__ = ["PovEngine"]