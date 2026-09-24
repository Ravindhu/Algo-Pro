"""
Orchestrator for the 4-engine trading system.

This module coordinates the sequential execution pipeline:
    Alpha → Risk → OMS → EMS

The orchestrator ensures safe data flow between engines, handles errors,
and provides a unified interface for the trading loop.

It supports hot-swapping of individual engines via the registry system.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from .interfaces import BaseAlphaEngine, BaseEMSEngine, BaseOMSEngine, BaseRiskEngine
from .registry import get_alpha_engine, get_ems_engine, get_oms_engine, get_risk_engine

logger = logging.getLogger(__name__)


class OrchestrationError(Exception):
    """Raised when the engine orchestration fails."""
    pass


class EngineOrchestrator:
    """
    Coordinates the 4-engine trading pipeline: Alpha → Risk → OMS → EMS.
    
    This is the main entry point for the trading system. It:
    1. Retrieves configured engine implementations from the registry
    2. Executes them in sequence with proper error handling
    3. Ensures data integrity between engine boundaries
    4. Provides detailed logging and error reporting
    
    The orchestrator is designed to be stateless and thread-safe.
    """

    def __init__(
        self,
        alpha_engine: str = "default",
        risk_engine: str = "default",
        oms_engine: str = "default",
        ems_engine: str = "default",
    ):
        """Initialize the orchestrator with engine names."""
        self.alpha_engine_name = alpha_engine
        self.risk_engine_name = risk_engine
        self.oms_engine_name = oms_engine
        self.ems_engine_name = ems_engine
        
        # Validate that all engines exist
        self._validate_engines()
        
        # Lazy-loaded engine instances
        self._alpha: Optional[BaseAlphaEngine] = None
        self._risk: Optional[BaseRiskEngine] = None
        self._oms: Optional[BaseOMSEngine] = None
        self._ems: Optional[BaseEMSEngine] = None
        
        logger.info(
            f"Initialized orchestrator with engines: "
            f"alpha={alpha_engine}, risk={risk_engine}, "
            f"oms={oms_engine}, ems={ems_engine}"
        )

    def _get_alpha(self) -> BaseAlphaEngine:
        """Lazy-load and return the Alpha engine instance."""
        if self._alpha is None:
            engine_cls = get_alpha_engine(self.alpha_engine_name)
            self._alpha = engine_cls
            logger.debug(f"Instantiated Alpha engine: {self.alpha_engine_name}")
        return self._alpha

    def _get_risk(self) -> BaseRiskEngine:
        """Lazy-load and return the Risk engine instance."""
        if self._risk is None:
            engine_cls = get_risk_engine(self.risk_engine_name)
            self._risk = engine_cls
            logger.debug(f"Instantiated Risk engine: {self.risk_engine_name}")
        return self._risk

    def _get_oms(self) -> BaseOMSEngine:
        """Lazy-load and return the OMS engine instance."""
        if self._oms is None:
            engine_cls = get_oms_engine(self.oms_engine_name)
            self._oms = engine_cls
            logger.debug(f"Instantiated OMS engine: {self.oms_engine_name}")
        return self._oms

    def run_cycle(
        self,
        symbol: str,
        exchange: str,
        client: Any,
        config: Dict[str, Any],
        risk_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute one complete cycle of the 4-engine pipeline."""
        result = {
            "success": False,
            "stage": None,
            "alpha_result": None,
            "risk_result": None,
            "oms_result": None,
            "ems_result": None,
            "error": None,
        }
        
        try:
            # Stage 1: Alpha - Generate signal
            logger.debug(f"Starting Alpha engine for {symbol}/{exchange}")
            alpha_engine = self._get_alpha()
            alpha_result = alpha_engine.calculate(symbol, exchange)
            result["alpha_result"] = alpha_result
            result["stage"] = "alpha"
            
            # Stage 2: Risk - Assess signal
            logger.debug("Starting Risk engine assessment")
            risk_engine = self._get_risk()
            risk_result = risk_engine.assess(alpha_result, risk_context or {})
            result["risk_result"] = risk_result
            result["stage"] = "risk"
            
            # Check if risk approved the signal
            if not risk_result.get("approved", False):
                result["error"] = (
                    f"Risk engine rejected signal: "
                    f"{risk_result.get('reason', 'Unknown')}"
                )
                logger.warning(result["error"])
                return result
            
            # Stage 3: OMS - Prepare order
            logger.debug("Starting OMS engine preparation")
            oms_engine = self._get_oms()
            oms_result = oms_engine.prepare(alpha_result, config)
            result["oms_result"] = oms_result
            result["stage"] = "oms"
            
            # Stage 4: EMS - Execute order
            logger.debug("Starting EMS engine execution")
            ems_engine = self._get_ems()
            ems_result = ems_engine.execute(oms_result)
            result["ems_result"] = ems_result
            result["stage"] = "ems"
            result["success"] = True
            
            # Update OMS with execution result
            try:
                oms_engine.update_position(ems_result)
            except Exception as e:
                logger.warning(f"Failed to update OMS position: {e}")
            
            logger.info(f"Successfully completed pipeline for {symbol}/{exchange}")
            
        except Exception as e:
            result["error"] = (
                f"Orchestration failed at stage '{result['stage']}': {str(e)}"
            )
            logger.error(result["error"], exc_info=True)
        
        return result

    def reset(self) -> None:
        """Reset all engine instances."""
        self._alpha = None
        self._risk = None
        self._oms = None
        self._ems = None
        logger.debug("Orchestrator engine instances reset")


def run_pipeline(
    symbol: str,
    exchange: str,
    client: Any,
    config: Dict[str, Any],
    alpha_engine: str = "default",
    risk_engine: str = "default",
    oms_engine: str = "default",
    ems_engine: str = "default",
    risk_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Convenience function to run the complete 4-engine pipeline."""
    orchestrator = EngineOrchestrator(
        alpha_engine=alpha_engine,
        risk_engine=risk_engine,
        oms_engine=oms_engine,
        ems_engine=ems_engine,
    )
    return orchestrator.run_cycle(symbol, exchange, client, config, risk_context)


__all__ = [
    "EngineOrchestrator",
    "OrchestrationError",
    "run_pipeline",
]