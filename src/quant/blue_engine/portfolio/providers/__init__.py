"""
providers - data adapters for the Portfolio engine (the ONLY I/O here).

Everything else in ``portfolio/`` is pure. Providers are the boundary that talks
to the OpenAlgo SDK (read-only) and assembles the canonical provider dict the
engine consumes::

    {
      "universe": ["NIFTY", "BANKNIFTY", "IT", ...],
      "vols":     {"NIFTY": 0.14, ...},          # annualised
      "corr":     {"NIFTY": {"BANKNIFTY": 0.8, ...}, ...},
      "betas":    {"NIFTY": 1.0, ...},
      "current_weights": {"NIFTY": 0.3, ...},    # from the live book
      "capital":  1_000_000.0,
      "meta": {"ok": bool, "stale": bool, "warnings": [str]}
    }

Modules
-------
base             PortfolioProvider ABC + TTL cache decorator (fail-soft)
universe         sleeve -> constituents mapping + default India universe
openalgo_provider builds vols/corr/betas/returns from client.history
account_provider builds current_weights/capital from positionbook/funds
composite        merge many providers into one dict
price_cache      persistent SQLite OHLCV store (the backtest dataset)
yfinance_provider optional key-free long-history backfill (degrade-away)
factory          ``portfolio_inputs()`` - the zero-arg provider_ref entry point

Design rules (mirror ``economics/providers``)
---------------------------------------------
* ``fetch()`` NEVER raises: failures are a partially-filled dict + ``meta.warnings``.
* No OpenAlgo internals imported — only the public SDK methods on an injected
  client (``client.history``, ``client.positionbook``, ``client.funds``).
* Credentials are resolved via ``economics.providers.secrets`` (env -> _restored/.env).
"""
from . import (account_provider, base, composite, factory, openalgo_provider,
               price_cache, universe, yfinance_provider)
from .base import CachedProvider, PortfolioProvider
from .composite import CompositeProvider
from .factory import (CacheProvider, StaticProvider, build_provider,
                      clear_client, get_client, portfolio_inputs, set_client)
from .price_cache import PriceCache
__all__ = ["base", "universe", "openalgo_provider", "account_provider",
           "composite", "price_cache", "yfinance_provider", "factory",
           "PortfolioProvider", "CachedProvider", "CompositeProvider",
           "PriceCache", "CacheProvider", "StaticProvider", "build_provider",
           "portfolio_inputs", "set_client", "get_client", "clear_client"]