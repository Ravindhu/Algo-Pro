"""Core utilities -- all OpenAlgo SDK style, no business logic."""

from __future__ import annotations

from blue_engine import _sdkpath  # noqa: F401
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
# LEGACY (audit F1): the 4-engine orchestrator below is deprecated.
# The canonical orchestration path is core/pipeline.MacroPipeline.
# Kept exported for backward compatibility only.
from .orchestrator import EngineOrchestrator, OrchestrationError, run_pipeline  # noqa: F401
from .risk_gate import RiskDecision, RiskGate, check_intent


def __getattr__(name: str):
    """Lazy exports to avoid a circular import (audit F1).

    ``pipeline`` imports the engine sub-packages, which import ``core`` —
    so importing it at module time here would loop. Resolving lazily keeps
    ``from blue_engine.core import MacroPipeline`` working for callers while
    breaking the cycle. ``run_pipeline``/``MacroPipeline`` resolve to the
    CANONICAL pipeline module, not the legacy orchestrator.
    """
    if name in ("MacroPipeline", "run_pipeline"):
        from . import pipeline as _pipeline
        return getattr(_pipeline, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

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
    "MacroPipeline",  # canonical orchestration
    "EngineOrchestrator",  # legacy, deprecated
    "OrchestrationError",  # legacy, kept for compat
    "RiskDecision",
    "RiskGate",
    "check_intent",
]
