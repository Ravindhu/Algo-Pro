"""
Core engine abstractions for the 4-engine trading system.

This module defines the pluggable architecture for:
- Alpha Engine (signal generation/analysis)
- Risk Engine (risk assessment & approval)
- OMS Engine (order management & position tracking)
- EMS Engine (execution management & slicing)

The architecture supports hot-swapping engines via a central registry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Type


class EngineType(Enum):
    """Types of engines in the 4-engine architecture."""
    ALPHA = "alpha"      # Signal generation / analysis
    RISK = "risk"        # Risk assessment & approval
    OMS = "oms"          # Order management (position tracking)
    EMS = "ems"          # Execution management (slicing)


@dataclass(frozen=True)
class EngineConfig(Dict[str, Any]):
    """Configuration container for an engine instance."""
    name: str
    parameters: Dict[str, Any]


class BaseAlphaEngine(ABC):
    """
    Alpha engine – responsible for generating or refining trading signals.
    
    This is analogous to a signal strategy but focuses specifically on
    analytical inputs (technical indicators, fundamental scores, etc.) that
    drive the POV loop's decision-making.
    """

    @abstractmethod
    def calculate(self, symbol: str, exchange: str) -> Dict[str, Any]:
        """
        Compute the alpha signal for the given symbol/exchange.
        
        Returns a dictionary containing at least:
        - 'score': numeric signal strength (higher = stronger buy/sell)
        - 'direction': 'BUY', 'SELL', or 'NEUTRAL'
        - 'confidence': optional confidence metric
        """
        ...


class BaseRiskEngine(ABC):
    """
    Risk engine – evaluates whether a signal is acceptable given risk constraints.
    
    Performs risk checks such as:
    - Position limits (max exposure per symbol)
    - Portfolio concentration
    - Drawdown thresholds
    - Correlation limits
    """

    @abstractmethod
    def assess(self, signal: Dict[str, Any], risk_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluate a signal against risk criteria.
        
        Args:
            signal: The incoming signal from the Alpha engine.
            risk_context: Additional risk context (portfolio state, etc.).
            
        Returns:
            A decision dict with keys like 'approved', 'reason', 'risk_score'.
        """
        ...


class BaseOMSEngine(ABC):
    """
    OMS (Order Management System) engine – manages order lifecycle and positions.
    
    Handles:
    - Tracking open/closed positions
    - Calculating remaining target quantities
    - Managing order state transitions
    - Ensuring compliance with position limits
    """

    @abstractmethod
    def prepare(self, signal: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Prepare an order request based on a signal and engine config.
        
        Returns a structured order request ready for the EMS engine.
        """
        ...

    @abstractmethod
    def update_position(self, order_result: Dict[str, Any]) -> None:
        """Update internal position tracking after an order is executed."""
        ...


class BaseEMSEngine(ABC):
    """
    EMS (Execution Management System) engine – executes orders in real time.
    
    Responsible for:
    - Translating prepared orders into actual exchanges calls
    - Handling order filling progress
    - Post-execution analytics (filled vs. target)
    - Dynamic block-size scaling based on liquidity profiles
    """

    @abstractmethod
    def execute(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a trading intent (BUY/SELL) and return execution summary.
        
        Args:
            intent: Dictionary with at least 'action' ('BUY'/'SELL') and 'quantity'.
            
        Returns:
            Execution summary containing filled quantity, target, child orders, etc.
        """
        ...

    @abstractmethod
    def calculate_max_child_qty(self, liquidity_profile: Any, remaining_qty: int, target_qty: int, pct: float) -> int:
        """
        Compute the maximum allowable child order quantity based on liquidity.
        
        This mirrors the existing `scale_block_size_pct` function but is exposed
        as a public API for the EMS engine.
        """
        ...

    @abstractmethod
    def post_process_child(self, response: Dict[str, Any], remaining_qty: int, target_qty: int) -> Dict[str, Any]:
        """
        Post-process a child order response to enforce liquidity caps.
        """
        ...
