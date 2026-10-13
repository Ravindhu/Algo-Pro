"""
pipeline.py - the master pipeline that chains the analytic and safety engines.

    Economic -> Market -> (Screener -> Valuation) -> Portfolio -> Alpha
       -> Risk -> POV -> Infrastructure

Each stage reads the outputs of the stages before it. The pipeline never
raises: a stage failure is recorded in that stage's :class:`EngineResult` and
(unless it is a hard gate) the pipeline continues with what it has.

Safety model
------------
* ``dry_run`` (default True) runs every analytic engine and builds the POV
  config, but does NOT submit orders. Live execution requires an explicit
  ``dry_run=False`` AND an OpenAlgo SDK client in the context.
* The Infrastructure engine's ``ready_to_trade`` can be configured to veto the
  POV stage (``gate_on_infrastructure=True``).
* No engine imports OpenAlgo internals. The SDK client is injected by the
  caller as ``context['client']``.

Usage::

    from blue_engine.core.pipeline import MacroPipeline
    pipe = MacroPipeline.from_config({"dry_run": True, ...})
    report = pipe.run(context={"macro": {...}, "market": {...}, ...})
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..alpha import AlphaEngine
from .base import EngineResult, EngineStage
from ..economics import EconomicEngine
from ..infrastructure import InfrastructureEngine
from ..market import MarketEngine
from ..portfolio import PortfolioEngine
from ..pov import PovEngine
from ..screener import ScreenerEngine, ValuationEngine
from ..sectors import SectorEngine
from .frontline_agent import FrontlineAgent
from .risk_engine import RiskEngine

logger = logging.getLogger(__name__)

# Default config lives under blue_engine/input/ (sibling of this core package).
DEFAULT_CONFIG_PATH = (Path(__file__).resolve().parent.parent
                       / "input" / "macro_master.json")


class MacroPipeline:
    """Ordered analytic chain with optional fundamental stages and risk veto."""

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None):
        self.config: Dict[str, Any] = dict(config or {})
        self._log_fn = log
        self.dry_run: bool = bool(self.config.get("dry_run", True))
        self.gate_on_infrastructure: bool = bool(
            self.config.get("gate_on_infrastructure", True))

        engines_cfg = self.config.get("engines") or {}
        self.frontline_agent = FrontlineAgent(engines_cfg.get("agent", {}))
        self.engines = [
            EconomicEngine(engines_cfg.get("economic", {}),
                           provider=self._provider("economic"), log=log),
            MarketEngine(engines_cfg.get("market", {}),
                         provider=self._provider("market"), log=log),
        ]
        sector_cfg = engines_cfg.get("sector") or {}
        if bool(sector_cfg.get("pipeline_enabled", False)):
            self.engines.append(SectorEngine(
                sector_cfg, provider=self._provider("sector"), log=log))
        screener_cfg = engines_cfg.get("screener") or {}
        if bool(screener_cfg.get("pipeline_enabled", False)):
            self.engines.append(ScreenerEngine(
                screener_cfg, provider=self._provider("screener"), log=log))
        valuation_cfg = engines_cfg.get("valuation") or {}
        if bool(valuation_cfg.get("pipeline_enabled", False)):
            self.engines.append(ValuationEngine(
                valuation_cfg, provider=self._provider("valuation"), log=log))
        self.engines.extend([
            PortfolioEngine(engines_cfg.get("portfolio", {}),
                            provider=self._provider("portfolio"), log=log),
            AlphaEngine(engines_cfg.get("alpha", {}),
                        provider=self._provider("alpha"), log=log),
            RiskEngine(engines_cfg.get("risk", {}),
                       provider=self._provider("risk"), log=log),
            PovEngine(engines_cfg.get("pov", {}),
                      provider=self._provider("pov"), log=log),
            InfrastructureEngine(engines_cfg.get("infrastructure", {}),
                                 provider=self._provider("infrastructure"),
                                 log=log),
        ])
        # Deterministic order by stage.
        self.engines.sort(key=lambda e: e.stage.value)

    # ---- provider resolution ------------------------------------------ #
    def _provider(self, key: str) -> Optional[Callable[[], Dict[str, Any]]]:
        """Resolve a provider callable from config.

        Config supplies dicts inline; a *callable* provider would be injected
        programmatically. We support ``{"provider": "module:function"}`` for
        a dotted path so a user can wire a live data adapter without editing
        engine code.
        """
        engines_cfg = self.config.get("engines") or {}
        block = engines_cfg.get(key) or {}
        ref = block.get("provider_ref")
        if not ref or ":" not in ref:
            return None
        module_name, func_name = ref.split(":", 1)
        try:
            import importlib
            module = importlib.import_module(module_name)
            return getattr(module, func_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not resolve provider_ref %s: %s", ref, exc)
            return None

    # ---- constructors -------------------------------------------------- #
    @classmethod
    def from_config(cls, config: Dict[str, Any],
                    log: Optional[Callable[[str], None]] = None) -> "MacroPipeline":
        return cls(config=config, log=log)

    @classmethod
    def from_config_file(cls, path: Optional[str] = None,
                         log: Optional[Callable[[str], None]] = None) -> "MacroPipeline":
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        if not p.is_file():
            raise FileNotFoundError(f"macro config not found: {p}")
        with p.open("r", encoding="utf-8") as fh:
            return cls(config=json.load(fh), log=log)

    # ---- execution ----------------------------------------------------- #
    def run(self, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run every engine in stage order and return a combined report."""
        ctx: Dict[str, Any] = dict(context or {})
        ctx.setdefault("dry_run", self.dry_run)

        report: Dict[str, Any] = {
            "dry_run": self.dry_run,
            "stages": {},
            "order": [e.name for e in self.engines],
            "warnings": [],
            "agent_advice": {},
            "ready_to_trade": None,
        }

        agent_inputs = ctx.get("agent_inputs")
        if self.frontline_agent.enabled and isinstance(agent_inputs, dict):
            for name in (
                    "economic", "market", "screener", "valuation",
                    "backtest", "optimization", "portfolio", "risk"):
                raw_input = agent_inputs.get(name)
                text = raw_input.get("text") if isinstance(raw_input, dict) else raw_input
                if not isinstance(text, str) or not text.strip():
                    continue
                advice = self.frontline_agent.advise(name, text)
                report["agent_advice"][name] = advice
                if advice.get("ok"):
                    if not isinstance(ctx.get("agent_advice"), dict):
                        ctx["agent_advice"] = {}
                    ctx["agent_advice"][name] = advice["advice"]
                else:
                    report["warnings"].append(
                        f"frontline agent {name}: {advice.get('warning', advice.get('method'))}")

        # Preflight: Infrastructure is the *last* analytic stage but its health
        # verdict gates the POV *execution*. We therefore evaluate it first for
        # its gate signal, then again in natural order for the report, so the
        # ordering stays top-down and the report is complete.
        infra_engine = next(
            (e for e in self.engines if e.stage is EngineStage.INFRASTRUCTURE), None)
        infrastructure_ok = True
        risk_veto_active = False
        if self.gate_on_infrastructure and infra_engine is not None:
            pre = infra_engine.run(ctx)
            infrastructure_ok = bool(pre.get("ready_to_trade", False))
            ctx["infrastructure"] = _as_ctx(pre)
            report["ready_to_trade"] = pre.get("ready_to_trade")
            report["warnings"].extend(pre.warnings)

        for engine in self.engines:
            # Skip the POV execution when infrastructure is not ready and the
            # gate is enabled. The analytic POV config is still built so the
            # operator can inspect it.
            if (engine.stage is EngineStage.POV
                    and (risk_veto_active or (
                        self.gate_on_infrastructure and not infrastructure_ok))):
                report["stages"][engine.name] = {
                    "engine": engine.name,
                    "ok": True,
                    "data": {"slice": {"status": "blocked",
                                       "reason": ("risk veto active"
                                                  if risk_veto_active else
                                                  "infrastructure not ready")}},
                    "warnings": [
                        ("POV blocked by risk gate" if risk_veto_active else
                         "POV blocked: infrastructure gate failed")],
                    "error": None,
                }
                report["warnings"].append(
                    "POV blocked by risk veto" if risk_veto_active else
                    "POV blocked by infrastructure gate")
                continue

            result = engine.run(ctx)
            ctx[engine.name] = _as_ctx(result)
            stage_result = dict(result.__dict__)
            if engine.name in report["agent_advice"]:
                stage_result["data"] = dict(result.data)
                stage_result["data"]["agent_advice"] = \
                    report["agent_advice"][engine.name]
            report["stages"][engine.name] = stage_result
            report["warnings"].extend(result.warnings)
            if engine.name == "risk":
                risk_veto_active = (
                    not result.ok or bool(result.get("risk_veto_active", True)))
                if risk_veto_active:
                    report["ready_to_trade"] = False
                    report["warnings"].append(
                        "POV blocked by risk veto or failed risk assessment")

        report["alpha_selected"] = (
            report["stages"].get("alpha", {}).get("data", {}).get("selected"))
        return report


def _as_ctx(result: EngineResult) -> Dict[str, Any]:
    """Adapt an EngineResult to the context sub-dict engines expect."""
    return {"ok": result.ok, "data": result.data, "warnings": result.warnings,
            "error": result.error}


def run_pipeline(config: Optional[Dict[str, Any]] = None,
                 context: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Convenience wrapper: build a pipeline from config and run it."""
    return MacroPipeline(config=config, log=log).run(context=context)


__all__ = ["MacroPipeline", "run_pipeline", "DEFAULT_CONFIG_PATH"]