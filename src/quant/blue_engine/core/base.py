"""
base.py - the shared contract every macro engine implements.

An *engine* here is a coarse, independently-testable decision stage. Each one
takes an immutable-ish input dict and returns an :class:`EngineResult`. Engines
are chained by :mod:`engines.pipeline`; each stage may read the outputs of the
stages before it.

Design rules
------------
* Pure by default: ``run()`` must not perform network or broker I/O unless the
  subclass explicitly opts in. Data acquisition belongs in a *provider* passed
  into the constructor (dependency injection), so engines are testable offline
  and swappable at runtime.
* No exceptions escape ``run()``: a failure is captured in
  ``EngineResult.ok=False`` so one broken macro feed cannot halt the pipeline.
  Fatal configuration errors are raised at construction time instead.
* Every engine declares a ``name`` and a ``stage`` so the pipeline can order and
  report them deterministically.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stage ordering
# ---------------------------------------------------------------------------
class EngineStage(Enum):
    """Top-down decision order of the six macro engines."""

    ECONOMIC = 10
    MARKET = 20
    SCREENER = 25
    VALUATION = 27
    PORTFOLIO = 30
    ALPHA = 40
    RISK = 45
    POV = 50
    INFRASTRUCTURE = 60


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------
@dataclass
class EngineResult:
    """The typed output of one engine stage.

    Attributes
    ----------
    engine : Name of the engine that produced this result.
    ok : True when the engine completed; False when it degraded.
    data : The engine's payload. Shape is engine-specific but always a dict.
    warnings : Human-readable non-fatal notes (missing feed, stale data, ...).
    error : Populated only when ``ok`` is False.
    """

    engine: str
    ok: bool = True
    data: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None

    @classmethod
    def failure(cls, engine: str, error: str,
                *, warnings: Optional[List[str]] = None) -> "EngineResult":
        return cls(engine=engine, ok=False, data={},
                   warnings=list(warnings or []), error=str(error))

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


# ---------------------------------------------------------------------------
# Base engine
# ---------------------------------------------------------------------------
class BaseEngine(abc.ABC):
    """Abstract base for every macro engine.

    Subclasses must set ``name`` and ``stage`` and implement :meth:`_run`.
    """

    name: str = "base"
    stage: EngineStage = EngineStage.ECONOMIC

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 provider: Any = None, log: Optional[Callable[[str], None]] = None):
        self.config: Dict[str, Any] = dict(config or {})
        #: Optional data provider (dependency injection). Analytic engines
        #: never require one; data-bound engines (market, economic) use it to
        #: fetch macro/market inputs without hard-coding a network client.
        self.provider = provider
        self._log_fn = log

    # ---- logging -------------------------------------------------------- #
    def log(self, message: str) -> None:
        if self._log_fn is not None:
            self._log_fn(f"[{self.name}] {message}")
        else:
            logger.info("[%s] %s", self.name, message)

    # ---- lifecycle ------------------------------------------------------ #
    def run(self, context: Optional[Dict[str, Any]] = None) -> EngineResult:
        """Execute the engine, never raising.

        ``context`` accumulates the outputs of earlier stages (and any shared
        state such as the OpenAlgo SDK client, universe, or risk budgets).
        """
        try:
            result = self._run(dict(context or {}))
            if not isinstance(result, EngineResult):
                raise TypeError(
                    f"{self.name}._run() must return EngineResult, "
                    f"got {type(result).__name__}"
                )
            return result
        except Exception as exc:  # noqa: BLE001 - deliberately broad
            self.log(f"FAILED: {exc!r}")
            return EngineResult.failure(self.name, repr(exc))

    @abc.abstractmethod
    def _run(self, context: Dict[str, Any]) -> EngineResult:
        """Engine-specific logic. Must return an :class:`EngineResult`."""
        raise NotImplementedError

    # ---- introspection -------------------------------------------------- #
    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "stage": self.stage.name,
            "config_keys": sorted(self.config.keys()),
        }


__all__ = ["BaseEngine", "EngineResult", "EngineStage"]