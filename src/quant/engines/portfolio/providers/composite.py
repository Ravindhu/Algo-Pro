"""
providers/composite.py - merge many providers into one portfolio input dict.

The engine consumes a single dict. Different sources contribute different keys:

    OpenAlgoPortfolioProvider  -> universe, vols, corr, betas, cov, returns
    AccountProvider            -> current_weights, capital, available_cash
    CacheProvider              -> universe, vols, corr, betas, cov, returns
    StaticProvider             -> universe, vols, corr, betas (config block)

Merge rules
-----------
* Dicts merge recursively (so ``current_weights`` from one source and ``vols``
  from another both survive).
* Scalars/lists: later source wins.
* ``meta.warnings`` are concatenated; ``meta.ok`` reflects whether the merged
  result can actually be used (i.e. it has a universe).

**Atomicity rule for the risk block (critical).**
``universe``, ``vols``, ``corr``, ``betas``, ``cov`` and ``returns`` are a
*single consistent set*: the keys must match. Deep-merging them across sources
therefore CORRUPTS the block - e.g. a cache keyed ``IT``/``PHARMA`` merged with
a config keyed ``NIFTYIT``/``NIFTYPHARMA`` yields six vols for a four-sleeve
universe, and the optimiser then silently reads missing keys as defaults.

**Data-quality precedence (also critical).** A source with an estimated
covariance (``cov``) is strictly more informative than one with hand-set vols,
and both beat a bare universe. So we do not simply let "the last source win" -
we score each universe-bearing source and let the *best* one own the risk block.
Otherwise a config fallback declaring 4 sleeves would wipe out the cache's real
covariance simply by being later in the list.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Set

from .base import PortfolioProvider

__all__ = ["CompositeProvider", "merge_dicts", "RISK_BLOCK_KEYS",
           "risk_block_quality"]


#: Keys that must come from one source as a consistent set (see module docstring).
RISK_BLOCK_KEYS: Set[str] = {
    "universe", "vols", "corr", "betas", "cov", "returns", "n_obs",
}


def risk_block_quality(payload: Dict[str, Any]) -> int:
    """Score a source's risk block: higher wins ownership of the block.

    0 = no universe (contributes nothing), 1 = a bare universe, 2 = hand-set
    vols, 3 = estimated covariance. This is what stops a config fallback from
    silently displacing a real covariance estimate.
    """
    if not payload.get("universe"):
        return 0
    if payload.get("cov"):
        return 3
    if payload.get("vols"):
        return 2
    return 1


def merge_dicts(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge ``overlay`` onto ``base`` (dicts recurse, else overlay wins)."""
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_dicts(out[k], v)
        else:
            out[k] = v
    return out


class CompositeProvider(PortfolioProvider):
    """Merge several providers into one canonical portfolio dict."""

    name = "composite"

    def __init__(self, providers: List[PortfolioProvider],
                 config: Optional[Dict[str, Any]] = None,
                 log: Optional[Callable[[str], None]] = None):
        super().__init__(config, log)
        self.providers = list(providers or [])

    def _fetch(self) -> Dict[str, Any]:
        merged: Dict[str, Any] = {}
        warnings: List[str] = []
        any_stale = False
        sources: List[str] = []
        risk_source: Optional[str] = None
        risk_quality = 0
        for p in self.providers:
            res = p.fetch()
            meta = res.pop("meta", {}) or {}
            name = meta.get("source", getattr(p, "name", "?"))
            sources.append(name)
            warnings.extend(meta.get("warnings", []))
            any_stale = any_stale or bool(meta.get("stale"))

            # Risk-block ownership by data quality, not by list position: an
            # estimated covariance beats hand-set vols beats a bare universe. A
            # losing source still contributes its NON-risk keys (current_weights,
            # capital, funds, ...), so nothing useful is discarded.
            q = risk_block_quality(res)
            if q > risk_quality:
                for k in RISK_BLOCK_KEYS:
                    merged.pop(k, None)
                merged = merge_dicts(merged, res)
                risk_quality = q
                risk_source = name
            elif q == 0:
                merged = merge_dicts(merged, res)
            else:
                # Weaker risk block: keep only its non-risk keys.
                merged = merge_dicts(
                    merged, {k: v for k, v in res.items()
                             if k not in RISK_BLOCK_KEYS})

        # Trim any orphaned risk-block keys: a value whose key set does not match
        # the winning universe is a corrupting leftover, so drop it.
        universe = merged.get("universe") or []
        if universe:
            allowed = set(universe)
            for key in ("vols", "corr", "betas"):
                block = merged.get(key)
                if isinstance(block, dict):
                    merged[key] = {k: v for k, v in block.items()
                                   if k in allowed}
            ret = merged.get("returns")
            if isinstance(ret, dict):
                merged["returns"] = {k: v for k, v in ret.items()
                                     if k in allowed}

        # Config-level overrides always win (operator intent).
        override = self.config.get("override")
        if isinstance(override, dict):
            merged = merge_dicts(merged, override)

        # ``ok`` means usable: a universe AND at least one risk measure.
        usable = bool(universe) and any(
            merged.get(k) for k in ("vols", "corr", "cov"))
        merged["meta"] = {
            "ok": usable, "stale": any_stale, "warnings": warnings,
            "sources": sources, "source": self.name,
            "risk_source": risk_source, "risk_quality": risk_quality,
        }
        self.log(f"merged {len(self.providers)} provider(s): {sources} "
                 f"risk_source={risk_source}(q={risk_quality}) ok={usable} "
                 f"stale={any_stale} universe={len(universe)}")
        return merged