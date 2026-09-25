"""
providers/factory.py - config-wireable entry point for the live portfolio inputs.

Why this exists
---------------
``core/pipeline.py`` resolves a provider from config via ``provider_ref``::

    "portfolio": { "provider_ref": "my.module:my_function" }

The resolved callable takes **no arguments** and returns the provider dict. So a
live provider needs a zero-argument factory that can find its own SDK client.

That is what :func:`portfolio_inputs` provides. It is the *single* place where the
portfolio sub-package reaches for a live client, and it degrades in a documented
order so the pipeline **never** fails to start because a broker is down.

Client resolution order
-----------------------
1. A client injected via :func:`set_client` (the runner/dashboard already owns
   one — reusing it avoids a second broker session).
2. ``BLUE_PORTFOLIO_CLIENT`` — a dotted ``module:attr`` path to a live object.
3. A freshly built client via ``core.client.init_client()`` when an
   ``OPENALGO_API_KEY`` is resolvable (env or ``_restored/.env``), proven usable
   by a lightweight read-only probe.
4. **No client** — fall back to the price cache, then to config inputs. The
   returned dict carries ``meta.ok=False`` and an explanatory warning, and the
   engine degrades to its legacy inverse-vol path.

Security
--------
The key is resolved only through ``core.client`` / ``economics.providers.secrets``
(env first, then ``_restored/.env``). It is never logged. This module never prints
the key, the account, or the raw funds payload.
"""
from __future__ import annotations

import importlib
import logging
import os
from typing import Any, Dict, List, Optional

from . import account_provider, composite, openalgo_provider, price_cache
from . import universe as universe_mod
from .base import PortfolioProvider

logger = logging.getLogger(__name__)

__all__ = ["portfolio_inputs", "set_client", "get_client", "clear_client",
           "build_provider", "CacheProvider", "StaticProvider"]


#: Module-level client cache. A process owns at most one broker session, so a
#: single slot is right — and it prevents accidental double logins.
_CLIENT: Optional[Any] = None


# --------------------------------------------------------------------------- #
# Client resolution
# --------------------------------------------------------------------------- #

def set_client(client: Any) -> None:
    """Inject an SDK client for subsequent :func:`portfolio_inputs` calls."""
    global _CLIENT
    _CLIENT = client


def clear_client() -> None:
    """Drop the cached client (tests / re-login)."""
    global _CLIENT
    _CLIENT = None


def get_client() -> Optional[Any]:
    """Return a usable SDK client, or None. Never raises."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT

    # 1. Explicit env hand-off (a dotted path to a live object).
    ref = os.getenv("BLUE_PORTFOLIO_CLIENT")
    if ref and ":" in ref:
        try:
            module_name, attr = ref.split(":", 1)
            module = importlib.import_module(module_name)
            candidate = getattr(module, attr)
            if candidate is not None:
                _CLIENT = candidate
                logger.info("portfolio client resolved from %s", ref)
                return _CLIENT
        except Exception as exc:  # noqa: BLE001 - misconfiguration is not fatal
            logger.warning("could not resolve BLUE_PORTFOLIO_CLIENT=%s: %r",
                           ref, exc)

    # 2. Build one from the API key (env -> _restored/.env).
    try:
        from ...core.client import init_client
        client = init_client()
        # The SDK constructs even with a None key, so prove it is usable before
        # caching it: a keyless environment must degrade, not pretend.
        if _probe(client):
            _CLIENT = client
            logger.info("portfolio client built from OPENALGO_API_KEY")
            return _CLIENT
        logger.info("SDK client built but not usable (no key / platform down)")
    except Exception as exc:  # noqa: BLE001 - degrade, never halt
        logger.info("could not build a portfolio client: %r", exc)
    return None


def _probe(client: Any) -> bool:
    """Cheaply check the client can reach the platform. Never raises.

    ``analyzerstatus`` is the lightest read-only endpoint the SDK exposes, so it
    is a good liveness probe. If the SDK lacks it we assume usable and let the
    provider's own calls decide.
    """
    if client is None:
        return False
    if not hasattr(client, "analyzerstatus"):
        return True
    try:
        resp = client.analyzerstatus()
    except Exception:  # noqa: BLE001
        return False
    if isinstance(resp, dict):
        return resp.get("status") in ("success", "error") or bool(resp)
    return True


# --------------------------------------------------------------------------- #
# Fallback providers
# --------------------------------------------------------------------------- #

class CacheProvider(PortfolioProvider):
    """Provide vols/corr/betas purely from the persisted price cache.

    This is the "broker is down" path. It is not a substitute for a live fetch —
    it is what keeps the risk model honest when the platform is unreachable.
    """

    name = "price_cache"

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 log: Optional[Any] = None):
        super().__init__(config, log)
        self._cache: Optional[price_cache.PriceCache] = None

    def _cache_obj(self) -> Optional[price_cache.PriceCache]:
        if self._cache is None:
            try:
                self._cache = price_cache.PriceCache(
                    self.config.get("cache_db"), log=self.log)
            except Exception as exc:  # noqa: BLE001
                self.log(f"price cache unavailable: {exc!r}")
                return None
        return self._cache

    def _fetch(self) -> Dict[str, Any]:
        cache = self._cache_obj()
        if cache is None:
            return {"meta": {"ok": False, "stale": True,
                             "warnings": ["price cache unavailable"]}}
        uni = universe_mod.resolve_universe(self.config)
        names = self.config.get("universe_names") or list(uni.keys())
        from ..risk import covariance as cov_mod
        from ..risk import beta as beta_mod

        closes: Dict[str, List[float]] = {}
        for sleeve in names:
            meta = uni.get(sleeve) or {}
            exch = meta.get("exchange", "NSE_INDEX")
            cs = cache.closes(sleeve, exch, "D")
            if len(cs) < 20:
                cs = cache.closes(sleeve, "YAHOO", "D")   # backfilled sleeve
            if len(cs) >= 20:
                closes[sleeve] = cs
        if len(closes) < 2:
            return {"meta": {"ok": False, "stale": True, "warnings": [
                "price cache holds fewer than 2 sleeves"]}}

        # Align and estimate exactly as the live provider does.
        present = sorted(closes.keys())
        tmin = min(len(closes[s]) for s in present)
        clipped = {s: closes[s][-tmin:] for s in present}
        rets = {s: [b / a - 1.0 for a, b in zip(clipped[s], clipped[s][1:])
                    if a > 0] for s in present}
        rt = min(len(rets[s]) for s in present)
        panel = [rets[s][-rt:] for s in present]

        est = cov_mod.estimate(panel, method=str(self.config.get(
            "cov_method", "ledoit_wolf")), periods=252.0)
        if not est.get("ok"):
            return {"meta": {"ok": False, "stale": True,
                             "warnings": ["cache covariance failed"]}}
        vols_map, corr_map = cov_mod.cov_to_dicts(est["cov"], present)
        bench = str(self.config.get("benchmark", "NIFTY"))
        betas: Dict[str, float] = {}
        if bench in present:
            bi = present.index(bench)
            for i, s in enumerate(present):
                b = beta_mod.ols_beta(panel[i], panel[bi])
                betas[s] = round(b, 4) if b is not None else 1.0
        else:
            betas = {s: 1.0 for s in present}
        return {
            "universe": present, "vols": vols_map, "corr": corr_map,
            "betas": betas, "cov": est["cov"], "n_obs": rt,
            "returns": {s: panel[i] for i, s in enumerate(present)},
            "meta": {"ok": True, "stale": True, "source": self.name,
                     "warnings": ["served from the price cache (not live)"]},
        }


class StaticProvider(PortfolioProvider):
    """Serve the config's ``portfolio_inputs`` block (the last-resort fallback)."""

    name = "static_config"

    def __init__(self, payload: Dict[str, Any], log: Optional[Any] = None):
        super().__init__({}, log)
        self._payload = dict(payload)

    def _fetch(self) -> Dict[str, Any]:
        return {**self._payload,
                "meta": {"ok": True, "stale": True, "source": self.name,
                         "warnings": ["served from config portfolio_inputs"]}}


# --------------------------------------------------------------------------- #
# Provider assembly
# --------------------------------------------------------------------------- #

def build_provider(config: Optional[Dict[str, Any]] = None, *,
                   client: Optional[Any] = None,
                   log: Optional[Any] = None) -> composite.CompositeProvider:
    """Assemble the live provider stack (risk + account + cache + config).

    Returns a ``CompositeProvider`` even when no client is available: the cache
    layer still contributes, and the composite reports ``meta.ok=False``/stale so
    the engine degrades knowingly rather than silently.
    """
    cfg = dict(config or {})
    use_client = client if client is not None else get_client()

    providers: List[PortfolioProvider] = []
    if use_client is not None:
        providers.append(openalgo_provider.OpenAlgoPortfolioProvider(
            use_client, cfg, log=log))
        providers.append(account_provider.AccountProvider(
            use_client, cfg, log=log))

    # Cache-only layer: contributes real history when the broker is unreachable.
    providers.append(CacheProvider(cfg, log=log))

    # Config-supplied inputs are the final, always-available fallback.
    fallback = cfg.get("portfolio_inputs")
    if isinstance(fallback, dict) and fallback:
        providers.append(StaticProvider(fallback, log=log))

    return composite.CompositeProvider(providers, config=cfg, log=log)


# --------------------------------------------------------------------------- #
# The zero-argument factory the pipeline resolves
# --------------------------------------------------------------------------- #

def portfolio_inputs() -> Dict[str, Any]:
    """Zero-argument provider factory for ``provider_ref``.

    Wire it in ``input/macro_master.json``::

        "portfolio": {
          "provider_ref": "blue_engine.portfolio.providers.factory:portfolio_inputs",
          "optimiser": "risk_parity",
          "rebalance_band_pct": 1.0
        }

    Returns the canonical provider dict. Never raises: on total failure it
    returns ``{"meta": {"ok": False, ...}}`` and the engine falls back to its own
    legacy path.

    Configuration is read from ``BLUE_PORTFOLIO_CONFIG`` (a JSON file path) when
    set, so the factory stays argument-free while still allowing per-deployment
    overrides (universe, benchmark, cache DB, cost rates).
    """
    cfg = _load_config()
    try:
        provider = build_provider(cfg)
        out = provider.fetch()
        meta = out.setdefault("meta", {})
        meta.setdefault("sources", [])
        # Provenance for the dashboard connection tab: did we get live data, the
        # cache, or config? An operator needs to know which without reading logs.
        if not out.get("universe"):
            meta["ok"] = False
            meta.setdefault("warnings", []).append(
                "no source produced a universe; engine will use its config path")
        return out
    except Exception as exc:  # noqa: BLE001 - the pipeline must never fail here
        logger.warning("portfolio_inputs factory failed: %r", exc)
        return {"meta": {"ok": False, "stale": False,
                         "warnings": [f"portfolio_inputs: {exc!r}"],
                         "source": "factory"}}


def _load_config() -> Dict[str, Any]:
    """Load the factory's config, merged over the macro-master portfolio block.

    Precedence: ``BLUE_PORTFOLIO_CONFIG`` JSON  >  the ``portfolio`` block of
    ``input/macro_master.json``  >  built-in defaults.

    Merging over the macro config means an operator sets the universe and cost
    knobs **once**, in the file they already edit, and the factory honours them.

    The macro config is located **relative to this package first** (so the answer
    does not depend on the process working directory - a job launched from
    ``Blue_engine/`` and one launched from the repo root must agree), then via
    CWD as a compatibility fallback.
    """
    import json
    from pathlib import Path

    cfg: Dict[str, Any] = {}

    # 1. The macro pipeline config's portfolio block (the usual place).
    macro_path = os.getenv("BLUE_MACRO_CONFIG_PATH")
    candidates: List[Path] = []
    if macro_path:
        candidates.append(Path(macro_path))
    else:
        # <repo>/Blue_engine/input/macro_master.json, resolved package-relative.
        # providers/factory.py -> portfolio -> Blue_engine -> repo root.
        here = Path(__file__).resolve()
        blue_engine_dir = here.parents[1]
        candidates.append(blue_engine_dir / "input" / "macro_master.json")
        # CWD-relative variants, for callers that pass a bare relative path.
        candidates.append(Path("blue_engine") / "input" / "macro_master.json")
        candidates.append(Path("input") / "macro_master.json")

    macro_file = next((p for p in candidates if p.is_file()), None)
    if macro_file is not None:
        try:
            with macro_file.open("r", encoding="utf-8") as fh:
                macro = json.load(fh)
            block = (macro.get("engines") or {}).get("portfolio")
            if isinstance(block, dict):
                # Drop the wiring keys - the factory does not consume them.
                cfg.update({k: v for k, v in block.items()
                            if k not in ("provider_ref", "provider")})
        except Exception as exc:  # noqa: BLE001 - a bad config is not fatal
            logger.warning("could not read %s: %r", macro_file, exc)
    else:
        logger.debug("no macro config found in %s", [str(c) for c in candidates])

    # 2. An explicit deployment override wins per key.
    path = os.getenv("BLUE_PORTFOLIO_CONFIG")
    if path:
        try:
            with Path(path).open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                cfg.update(data)
            else:
                logger.warning("BLUE_PORTFOLIO_CONFIG is not a JSON object; ignored")
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read BLUE_PORTFOLIO_CONFIG=%s: %r",
                           path, exc)
    return cfg