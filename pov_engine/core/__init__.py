"""Core utilities -- all OpenAlgo SDK style, no business logic."""

from __future__ import annotations

from pov_engine import _sdkpath  # noqa: F401
_sdkpath.ensure_openalgo_sdk_path()

from .client import init_client
from .mode import read_mode
from .time_utils import ist_now, parse_hhmm, is_past_cutoff
from .volume_cache import subscribe as subscribe_volume_cache
from .volume_cache import get_quote as get_quote_from_cache
from .volume_cache import cache_snapshot
from .interfaces import BaseAlphaEngine, BaseRiskEngine, BaseOMSEngine, BaseEMSEngine
from .registry import (
    register_alpha_engine,
    register_risk_engine,
    register_oms_engine,
    register_ems_engine,
    get_alpha_engine,
    get_risk_engine,
    get_oms_engine,
    get_ems_engine,
    list_engines,
    is_registered,
    unregister_engine,
    clear_registry,
    discover_all,
)
from .orchestrator import EngineOrchestrator, OrchestrationError, run_pipeline

__all__ = [
    "init_client",
    "read_mode",
    "ist_now",
    "parse_hhmm",
    "is_past_cutoff",
    "subscribe_volume_cache",
    "get_quote_from_cache",
    "cache_snapshot",
    "BaseAlphaEngine",
    "BaseRiskEngine",
    "BaseOMSEngine",
    "BaseEMSEngine",
    "register_alpha_engine",
    "register_risk_engine",
    "register_oms_engine",
    "register_ems_engine",
    "get_alpha_engine",
    "get_risk_engine",
    "get_oms_engine",
    "get_ems_engine",
    "list_engines",
    "is_registered",
    "unregister_engine",
    "clear_registry",
    "discover_all",
    "EngineOrchestrator",
    "OrchestrationError",
    "run_pipeline",
]
