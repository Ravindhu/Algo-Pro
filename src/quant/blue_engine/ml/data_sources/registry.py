"""
ml/data_sources/registry.py — the plug-and-unplug table of every data source.

WHY A REGISTRY INSTEAD OF DIRECT IMPORTS
----------------------------------------
AGENTS.md asks for the OpenAlgo path to be "upgrade proof, easy plug and unplug".
The same principle applies to data. A registry gives us:

  * **Lazy construction** — importing `blue_engine.ml` must not import `yfinance`,
    `torch` or hit the network. Each source is only built when actually asked
    for, so the dashboard starts instantly and a machine without a broker still
    works.
  * **Discoverability** — the dashboard ML page lists every source with its
    availability, without importing any of them. One table, one place to look.
  * **Config-driven selection** — a training run names sources as strings
    ("price_cache", "macro"), so adding a source needs no change to the trainer.

HOW TO ADD A SOURCE (the 3-line recipe)
---------------------------------------
    1. write `ml/data_sources/<name>.py` with a `DataSource` subclass,
    2. add one line to `SOURCES` below,
    3. add a pinned-value test in `Blue_engine/tests/test_ml_data_sources.py`.

Nothing else in the codebase needs to change. That is the plug-and-unplug test:
if adding a source requires editing the trainer, the design is wrong.

WHY `_LAZY` MAPS NAMES TO STRINGS AND NOT CLASSES
-------------------------------------------------
Importing every source class at module import would pull in the whole portfolio
and economics packages — including their optional dependencies — every time
anyone touches `blue_engine.ml`. Mapping to a "module:Class" string keeps import
cost at zero and makes a broken optional dependency a *runtime* warning rather
than an *import-time* crash. It also means a source can be deleted and the
registry degrades to "unavailable" instead of raising ImportError.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import logger
from ..audit.degradation import DegradationLog
from .base import DataSource

__all__ = [
    "SOURCES", "build_source", "available_sources", "describe_all",
    "resolve_names",
]


#: Canonical name -> "module:ClassName". Order is the default training order:
#: price first (it is always available), then macro, then optional research feeds.
SOURCES: Dict[str, str] = {
    # --- always available (offline, no credentials) ----------------------- #
    "price_cache": "price_cache:PriceCacheSource",
    # --- broker-fed (needs a running OpenAlgo platform) ------------------- #
    "openalgo": "openalgo_source:OpenAlgoSource",
    # --- official / free public sources ----------------------------------- #
    "macro": "macro_source:MacroSource",
    "yfinance": "yfinance_source:YFinanceSource",
    # --- file-backed (a collector or a human supplies the file) ------------ #
    "news": "news_source:NewsSource",
}


#: Default sources a first training run should try, in priority order. Chosen so
#: a brand-new install with only the price cache still produces something honest
#: rather than failing on a source that was never configured.
DEFAULT_SOURCES: List[str] = ["price_cache", "openalgo", "macro"]


def build_source(name: str, config: Optional[Dict[str, Any]] = None,
                 log: Optional[DegradationLog] = None) -> Optional[DataSource]:
    """Construct one source by name. Returns None when it cannot be built.

    Returns None (rather than raising) because a missing *optional* source is a
    normal state — e.g. `news` with no collector configured, or `yfinance` not
    installed. The caller decides whether that is fatal for its run; the registry
    only reports the fact.

    The returned instance is NOT checked for `available()`: construction and
    availability are different questions ("can we instantiate it?" vs "can it
    actually fetch?"). A trainer usually wants to build first, then ask.
    """
    ref = SOURCES.get(name)
    if not ref:
        logger.warning("unknown ML data source %r; known: %s",
                       name, sorted(SOURCES))
        return None

    module_name, _, class_name = ref.partition(":")
    try:
        from importlib import import_module

        # Relative to this package, so the registry keeps working if the package
        # is renamed or vendored elsewhere.
        module = import_module(f".{module_name}", package=__package__)
        cls = getattr(module, class_name)
    except Exception as exc:  # noqa: BLE001 - optional dependency boundary
        logger.warning("could not import ML source %s (%s): %r",
                       name, ref, exc)
        return None

    try:
        return cls(config=config or {}, log=log)
    except Exception as exc:  # noqa: BLE001 - a bad config must not crash a run
        logger.warning("could not construct ML source %s: %r", name, exc)
        return None


def available_sources(log: Optional[DegradationLog] = None,
                      config: Optional[Dict[str, Dict[str, Any]]] = None
                      ) -> Dict[str, bool]:
    """``{name: is_available}`` for every registered source.

    Never raises. An unbuildable source reports False rather than vanishing, so
    the dashboard can show "news — not configured" instead of silently omitting a
    row an operator might be looking for.
    """
    out: Dict[str, bool] = {}
    cfg = config or {}
    for name in SOURCES:
        src = build_source(name, cfg.get(name), log)
        if src is None:
            out[name] = False
            continue
        try:
            out[name] = bool(src.available())
        except Exception:  # noqa: BLE001 - an availability probe must not raise
            out[name] = False
    return out


def describe_all(config: Optional[Dict[str, Dict[str, Any]]] = None,
                 log: Optional[DegradationLog] = None) -> List[Dict[str, Any]]:
    """Rich per-source description for the dashboard's ML page.

    Each entry carries enough for a UI table without further calls:
        name, kind, available, ref, required_columns, config
    """
    cfg = config or {}
    rows: List[Dict[str, Any]] = []
    for name, ref in SOURCES.items():
        entry: Dict[str, Any] = {"name": name, "ref": ref,
                                 "kind": "unknown", "available": False,
                                 "required_columns": [], "config": cfg.get(name) or {}}
        src = build_source(name, cfg.get(name), log)
        if src is not None:
            try:
                d = src.describe()
                entry.update({"kind": d.get("kind", "unknown"),
                              "required_columns": d.get("required_columns", []),
                              "available": bool(src.available())})
            except Exception:  # noqa: BLE001
                pass
        rows.append(entry)
    return rows


def resolve_names(names: Optional[List[str]] = None, *,
                  only_available: bool = False,
                  config: Optional[Dict[str, Dict[str, Any]]] = None,
                  log: Optional[DegradationLog] = None) -> List[str]:
    """Turn a user-supplied source list into a validated, de-duplicated list.

    Rules, all of which exist to prevent a silent no-op training run:

      * ``None`` -> :data:`DEFAULT_SOURCES`.
      * an unknown name is dropped **with a warning** (never silently).
      * order is preserved, duplicates removed — training order affects merge
        precedence, so we must not sort it alphabetically.
      * with ``only_available=True``, sources that cannot fetch are dropped.
        Used by the CLI's `--strict` mode to fail fast, and by the dashboard to
        show what *would* actually run.
    """
    requested = list(names) if names else list(DEFAULT_SOURCES)

    seen: set = set()
    ordered: List[str] = []
    for n in requested:
        if n in seen:
            continue
        seen.add(n)
        if n not in SOURCES:
            logger.warning("dropping unknown ML source %r (known: %s)",
                           n, sorted(SOURCES))
            continue
        ordered.append(n)

    if not only_available:
        return ordered

    avail = available_sources(log=log, config=config)
    kept = [n for n in ordered if avail.get(n)]
    dropped = [n for n in ordered if not avail.get(n)]
    if dropped:
        logger.warning("dropping unavailable ML sources: %s", dropped)
    return kept