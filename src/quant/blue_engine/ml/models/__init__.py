"""
ml/models/__init__.py — the candidate table: every model the trainer may run.

WHY A TABLE AND NOT DIRECT IMPORTS
----------------------------------
Same rule as ``data_sources/registry.py``: importing ``blue_engine.ml.models``
must not import torch or sklearn. Every entry here is a *lazy* reference — the
build function is only called when the trainer asks for that candidate, so:

  * the dashboard starts instantly on a machine with no torch,
  * a missing optional dependency becomes a per-candidate ``ok=False``
    with a REASON (see ``base.ModelSpec``), never an import-time crash,
  * adding a candidate is a one-line change here — nothing else edits.

HOW THE TRAINER USES IT
-----------------------
``ALL_CANDIDATES`` is the default search set. The trainer builds each one,
records ``ok``/``reason``, and runs the walk-forward on the ones that built.
Config can narrow it: ``{"models": ["ridge", "gbm"]}`` in the run block.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from .base import ModelSpec
from . import base, cnn, ensemble, gradient_boosting, mlp, random_forest, ridge, rl

__all__ = [
    "ALL_CANDIDATES", "build_candidate", "resolve_candidates",
    "ModelSpec", "base",
]

#: name -> (module, build-function name). Lazy, like the data-source registry.
ALL_CANDIDATES: Dict[str, str] = {
    "ridge": "ridge:build",
    "gbm": "gradient_boosting:build",
    "rf": "random_forest:build",
    "mlp": "mlp:build",
    "cnn": "cnn:build",
    "rl": "rl:build",
    "ensemble": "ensemble:build",
}

#: The default search order. Ridge first because it is the baseline every
#: other candidate is judged against; torch families last because they are
#: the most expensive and the first to be skipped when unavailable.
DEFAULT_CANDIDATES: List[str] = ["ridge", "gbm", "rf", "ensemble"]


def build_candidate(name: str, **over) -> ModelSpec:
    """Build one candidate by name. Returns a ModelSpec with ok/reason set."""
    ref = ALL_CANDIDATES.get(name)
    if not ref:
        return ModelSpec(name=name, family="unknown", ok=False,
                         reason=f"unknown candidate {name!r}; known: "
                                f"{sorted(ALL_CANDIDATES)}")
    module_name, _, fn_name = ref.partition(":")
    try:
        from importlib import import_module

        module = import_module(f".{module_name}", package=__package__)
        fn: Callable[..., ModelSpec] = getattr(module, fn_name)
    except Exception as exc:  # noqa: BLE001 - optional-dependency boundary
        return ModelSpec(name=name, family="unknown", ok=False,
                         reason=f"import failed: {exc!r}")
    try:
        return fn(**over)
    except Exception as exc:  # noqa: BLE001 - a bad param must not kill the search
        return ModelSpec(name=name, family="unknown", ok=False,
                         reason=f"build failed: {exc!r}")


def resolve_candidates(names: Optional[List[str]] = None, *,
                       only_ok: bool = True, **over) -> List[ModelSpec]:
    """Turn a requested candidate list into built ModelSpecs, in order.

    ``None`` -> :data:`DEFAULT_CANDIDATES`. Unknown names are dropped with a
    logged reason (never silently). With ``only_ok=True``, candidates whose
    dependencies are missing are also dropped — each with its reason logged,
    so the run manifest records WHY a family was absent.
    """
    from .. import logger

    requested = list(names) if names else list(DEFAULT_CANDIDATES)
    out: List[ModelSpec] = []
    seen = set()
    for n in requested:
        if n in seen:
            continue
        seen.add(n)
        spec = build_candidate(n, **over)
        if not spec.ok:
            logger.warning("candidate %s unavailable: %s", n, spec.reason)
            if only_ok:
                continue
        out.append(spec)
    return out
