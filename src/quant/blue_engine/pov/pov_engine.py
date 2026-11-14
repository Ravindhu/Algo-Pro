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
    """Map the upstream decisions onto a POVSlicer config dict.

    NOTE (audit 2026-10-22): this used to scale ``participation_pct`` by the
    portfolio's ``gross_exposure_multiplier``. That key does not exist on the
    Portfolio payload (it lives on the Economic payload and the portfolio
    RESULT is not even passed into this function), so the block was dead code.
    Gross exposure is now applied once, in the Portfolio engine, by
    ``PortfolioEngine._economic_tilt``; participation rate stays an execution
    decision so the same book is not de-risked twice.
    """
    cfg = dict(base)
    cfg["symbol"] = symbol
    cfg["exchange"] = exchange
    cfg["action"] = action
    cfg["quantity"] = int(quantity)
    return cfg


def _economic_execution_guard(portfolio: Dict[str, Any],
                             quantity: int) -> Dict[str, Any]:
    """Carry the Portfolio's economic tilt into the execution budget.

    The Portfolio scales WEIGHTS by the Economic ``gross_exposure_multiplier``.
    Execution, however, consumes a share count passed in the context or in the
    POV config, so without this bridge the multiplier is invisible at the point
    where money is actually committed. The guard is reported in the slice
    payload so the dashboard can show exactly what was trimmed and why.
    """
    if not isinstance(portfolio, dict) or quantity <= 0:
        return {"applied": False, "reason": "no portfolio context"}
    tilt = portfolio.get("economic_tilt")
    if not isinstance(tilt, dict) or not tilt.get("applied"):
        return {"applied": False,
                "reason": (tilt or {}).get("reason") or "economic tilt inactive"}
    mult = safe_float(tilt.get("multiplier"))
    if mult is None or mult <= 0:
        return {"applied": False, "reason": "invalid multiplier"}
    allowed = int(quantity * min(1.0, mult))
    return {
        "applied": True,
        "multiplier": round(mult, 4),
        "requested_quantity": int(quantity),
        "allowed_quantity": allowed,
        "regime": tilt.get("regime"),
        "risk_on_score": tilt.get("risk_on_score"),
    }


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

    @staticmethod
    def _sleeve_quantity_cap(portfolio: Dict[str, Any],
                             symbol: str) -> Optional[int]:
        """Quantity implied by the portfolio weight for ``symbol``.

        Returns None when the portfolio block carries no usable capital/price,
        in which case no cap is applied (identical to the previous behaviour).
        A symbol that is absent from ``target_weights`` yields 0, i.e. an
        explicit refusal to trade a name the portfolio does not hold.
        """
        weights = portfolio.get("target_weights") if isinstance(portfolio, dict) else None
        if not isinstance(weights, dict) or not weights:
            return None
        capital = safe_float(portfolio.get("capital"))
        price = safe_float(portfolio.get("price")) or safe_float(
            portfolio.get("ltp")) or safe_float(portfolio.get("last_price"))
        if capital is None or capital <= 0 or price is None or price <= 0:
            return None
        weight = safe_float(weights.get(symbol))
        if weight is None:
            weight = safe_float(weights.get(str(symbol).upper()))
        if weight is None:
            # Sleeve names in the config are composites ("IT", "NIFTYIT"); an
            # exact miss must not silently mean "no cap" when the book is
            # clearly non-empty.
            return 0
        # NOTE: capital x weight is the sleeve budget; it must NOT be scaled by
        # the economic multiplier again. The multiplier was already applied to
        # the weights by PortfolioEngine._economic_tilt, so scaling here would
        # de-risk the same book twice.
        return int((max(0.0, weight) * capital) // price)

    @staticmethod
    def _budget_quantity_cap(portfolio: Dict[str, Any]) -> Optional[int]:
        """Shares buyable with the book's CASH BUDGET (1 - cash_weight).

        Used only when the portfolio's sleeve names cannot be mapped onto the
        execution symbol. It never grants more than the book's documented
        investable budget.
        """
        capital = safe_float(portfolio.get("capital"))
        price = safe_float(portfolio.get("price")) or safe_float(
            portfolio.get("ltp")) or safe_float(portfolio.get("last_price"))
        if capital is None or capital <= 0 or price is None or price <= 0:
            return None
        cash_weight = safe_float(portfolio.get("cash_weight"))
        gross = safe_float(portfolio.get("gross"))
        if gross is not None and gross > 0:
            budget_weight = min(gross, 1.0)
        elif cash_weight is not None:
            budget_weight = max(0.0, 1.0 - cash_weight)
        else:
            return None
        return int((budget_weight * capital) // price)

    def _resolve_slicer(self, client: Any, pov_cfg: Dict[str, Any]):
        if self._slicer_factory is not None:
            return self._slicer_factory(client, pov_cfg)
        # Lazy import keeps the engines package SDK-free at import time.
        from blue_engine.pov.slicers.pov import POVSlicer
        return POVSlicer(client, pov_cfg)

    # ---- 1. slicing ---------------------------------------------------- #
    def _slice(self, context: Dict[str, Any], warnings: List[str]):
        client = context.get("client")
        portfolio: Dict[str, Any] = {}
        if isinstance(context.get("portfolio"), dict):
            nested = context["portfolio"].get("data")
            portfolio = nested if isinstance(nested, dict) else context["portfolio"]

        # Choose the symbol/exchange: explicit order in the context wins,
        # else the first ALPHA-eligible candidate. The old fallback used
        # ``selected[0]`` blindly, so if the alpha stage
        # produced nothing the slicer still opened a random position with the
        # configured default BUY.
        symbol = context.get("symbol")
        exchange = context.get("exchange")
        action = context.get("action")
        quantity = context.get("quantity")

        alpha: Dict[str, Any] = {}
        if isinstance(context.get("alpha"), dict):
            nested = context["alpha"].get("data")
            alpha = nested if isinstance(nested, dict) else context["alpha"]
        if not symbol:
            selected = alpha.get("selected") or []
            if not selected:
                warnings.append(
                    "POV not executed: alpha selected no eligible candidate "
                    "(fail-closed; buy the default symbol is not a substitute "
                    "for a signal)")
                return {"status": "skipped",
                        "reason": "no alpha-selected symbol",
                        "alpha_candidates": len(alpha.get("candidates") or [])}, warnings
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
        # Portfolio sleeves are regime indices ("NIFTY", "NIFTYIT") while an
        # execution targets a stock ("INFY"). The sleeve lookup therefore has
        # THREE outcomes, which the old code collapsed into two:
        #   1. symbol found          -> cap at the sleeve allocation
        #   2. book non-empty, name  -> the sleeve is un-mappable; fall back to
        #      absent (composite)      the book's investable BUDGET and warn
        #   3. empty allocation      -> refuse (fail closed)
        # Audit (2026-10-22): case 2 used to return "skip", making the whole
        # Economic -> POV money-path non-functional for stock execution.
        weight_cap = self._sleeve_quantity_cap(portfolio, symbol)
        if weight_cap == 0:
            budget = self._budget_quantity_cap(portfolio)
            if budget is not None and budget > 0:
                warnings.append(
                    f"portfolio sleeves {sorted(portfolio.get('target_weights'))} "
                    f"do not name {symbol}; capping to the portfolio budget "
                    f"of {budget} shares instead of the sleeve allocation")
                weight_cap = budget
        # The sleeve size derived from the portfolio weights is carried as a
        # CEILING: an execution order may never exceed the allocation the
        # portfolio approved for that symbol. Missing weights = no cap.
        if weight_cap is not None:
            if weight_cap <= 0:
                warnings.append(
                    f"POV not executed: portfolio allocates no weight to {symbol}")
                return {"status": "skipped",
                        "reason": "symbol not in target portfolio",
                        "symbol": symbol}, warnings
            if quantity > weight_cap:
                warnings.append(
                    f"POV quantity {quantity} capped to portfolio allocation "
                    f"{weight_cap} for {symbol}")
                quantity = weight_cap
            cfg["quantity"] = int(quantity)

        # Economic -> execution guard (audit 2026-10-22): the economic tilt only
        # changed the WEIGHTS, and POVSlicer consumes cash quantity, so a
        # RISK_OFF book and a RISK_ON book executed the same order. The tilt is
        # now carried into the slice as an explicit, reported cap.
        economic_guard = _economic_execution_guard(portfolio, quantity)
        if economic_guard.get("applied"):
            cfg["economic_guard"] = economic_guard
            if economic_guard["allowed_quantity"] < quantity:
                warnings.append(
                    f"POV quantity {quantity} reduced to "
                    f"{economic_guard['allowed_quantity']} by economic "
                    f"multiplier {economic_guard['multiplier']}"
                    f" (regime={economic_guard.get('regime')})")
                quantity = economic_guard["allowed_quantity"]
                cfg["quantity"] = int(quantity)
            if quantity <= 0:
                warnings.append(
                    "POV not executed: economic tilt reduces the allowed "
                    "quantity to zero")
                return {"status": "skipped",
                        "reason": "economic_tilt_blocks_order",
                        "symbol": symbol,
                        "economic_guard": economic_guard}, warnings

        # AUDIT (2026-10-22, F-17): ``dry_run`` used to be an unimplemented
        # convention. ``MacroPipeline`` documents that ``dry_run=True`` submits
        # nothing, but this engine only ever looked at ``client is None``. A
        # caller that supplied a client to a dry-run pipeline therefore placed
        # REAL orders. The flag is now an explicit gate that the client cannot
        # override: dry_run wins over a live client. Live orders require BOTH
        # ``dry_run=False`` AND a client.
        if bool(context.get("dry_run", True)):
            warnings.append("dry_run is active; slice config built but no order submitted")
            return {
                "status": "dry_run",
                "reason": "dry_run flag is set",
                "config": cfg,
            }, warnings

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