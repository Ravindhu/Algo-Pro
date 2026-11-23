"""
portfolio_engine.py - the Portfolio engine (thin orchestrator).

Turns the Economic regime + Market picture into an *allocation*, using the full
quant stack built in this package:

  1. **providers**   assemble the inputs (vols, corr, betas, current book) — the
                     only place that touches the SDK (via an injected client).
  2. **risk**        estimate the covariance, forecast vol, tail risk, betas and
                     component risk contributions.
  3. **views**       optional ML / NLP / LLM expected-return views (opt-in).
  4. **optim**       construct target weights (risk parity / HRP / min-var /
                     max-Sharpe / Black-Litterman / mean-CVaR) with constraints.
  5. **sizing**      scale gross via vol-target, fractional Kelly and a drawdown
                     governor.
  6. **cost_aware**  apply the India cost model and a no-trade region.
  7. **attribution** drift + performance + Brinson (when data allows).
  8. **hedge**       size a residual-beta hedge.

This file stays **pure** by contract (``core/base.py``): no network, no SDK, no
DB in ``_run()``. All data arrives via the injected ``provider`` (a callable) or
``context['portfolio_inputs']``. The heavy lifting lives in the sub-packages,
each of which degrades gracefully when its optional dependency is missing.

Output contract (``EngineResult.data``) — superset of the original, so downstream
engines and the dashboard keep working unchanged::

    {
      "target_weights": {...}, "rebalance_trades": [...],
      "risk": {...}, "hedge": {...}, "capital": ..., "cash_weight": ...,
      # NEW (all optional, present when the data allows):
      "optimiser": "hrp", "gross": 1.32, "constraints": {...},
      "sizing": {"vol_target": {...}, "kelly": {...}, "drawdown": {...},
                 "combined_scale": 0.94},
      "views": [...], "view_sources": {...},
      "cost": {"turnover": .., "cost_bps": .., "cost_amount": ..},
      "attribution": {...}, "risk_model": {...}, "diagnostics": {...},
    }
"""
from __future__ import annotations

import logging
import math
from typing import Any, Callable, Dict, List, Optional

from ..core.base import BaseEngine, EngineResult, EngineStage
from ..core.results import clamp, safe_float
from . import attribution as attr
from . import optim as optim_pkg
from . import risk as risk_pkg
from . import sizing as sizing_pkg
from . import views as views_pkg
from .providers import universe as universe_mod

logger = logging.getLogger(__name__)


class PortfolioEngine(BaseEngine):
    """Construct, size, risk-check, cost-check and hedge the target book."""

    name = "portfolio"
    stage = EngineStage.PORTFOLIO

    def __init__(self, config: Optional[Dict[str, Any]] = None,
                 provider: Optional[Callable[[], Dict[str, Any]]] = None,
                 **kwargs: Any):
        super().__init__(config, provider, kwargs.get("log"))
        cfg = self.config

        # --- construction / constraints -------------------------------- #
        self.max_weight: float = float(cfg.get("max_weight", 0.40))
        self.min_weight: float = float(cfg.get("min_weight", 0.0))
        self.max_gross: float = float(cfg.get("max_gross", 1.0))
        self.max_tech_sector_exposure = self._limit_config(
            "max_tech_sector_exposure", cfg.get("max_tech_sector_exposure", 0.20))
        self.min_cash_buffer_pct = self._limit_config(
            "min_cash_buffer_pct", cfg.get("min_cash_buffer_pct", 0.15))
        self.max_turnover: Optional[float] = (
            None if cfg.get("max_turnover") is None
            else float(cfg["max_turnover"]))
        self.beta_lower: Optional[float] = (
            None if cfg.get("beta_lower") is None else float(cfg["beta_lower"]))
        self.beta_upper: Optional[float] = (
            None if cfg.get("beta_upper") is None else float(cfg["beta_upper"]))

        #: explicit optimiser, or "auto" to pick from the Economic regime.
        self.optimiser: str = str(cfg.get("optimiser", "auto")).lower()
        self.leader_tilt: float = float(cfg.get("leader_tilt", 0.0))
        self.risk_model: str = str(cfg.get("risk_model", "ledoit_wolf"))
        self.cov_method: str = str(cfg.get("cov_method", "ledoit_wolf"))

        # --- rebalancing ----------------------------------------------- #
        self.rebalance_band: float = float(
            cfg.get("rebalance_band_pct", 5.0)) / 100.0
        self.cost_band_pct: float = float(cfg.get("cost_band_pct", 2.0))

        # --- sizing ---------------------------------------------------- #
        self.target_vol: Optional[float] = safe_float(cfg.get("target_vol"))
        self.sizing_enabled: bool = bool(cfg.get("sizing_enabled", True))
        self.use_kelly: bool = bool(cfg.get("use_kelly", True))
        self.use_drawdown: bool = bool(cfg.get("use_drawdown_governor", True))

        # --- hedge ----------------------------------------------------- #
        self.hedge_beta_tolerance: float = float(
            cfg.get("hedge_beta_tolerance", 0.10))
        self.max_hedge_ratio: float = float(cfg.get("max_hedge_ratio", 0.60))
        self.benchmark: str = str(cfg.get("benchmark", "NIFTY"))

        # --- views (opt-in) -------------------------------------------- #
        self.views_enabled: bool = bool(cfg.get("views_enabled", False))

        # --- economic-consume (audit fix 2026-10-22, P0-3) --------------- #
        #: The Economic engine publishes ``gross_exposure_multiplier`` as the
        #: documented tilt for the Portfolio engine, but NOTHING used it: the
        #: regime reached the optimiser only through the regime->policy map, so
        #: a RISK_OFF multiplier of 0.74 produced the same gross as RISK_ON.
        #: When enabled, the target book is scaled by the multiplier and the
        #: result is reported under ``economic_tilt`` for audit.
        self.consume_gross_multiplier: bool = bool(
            cfg.get("consume_gross_multiplier", True))

        # --- persistence ----------------------------------------------- #
        self.persist: bool = bool(cfg.get("persist", False))

        # stateful helpers (remember previous scale / high-water mark)
        self._vol_target = sizing_pkg.vol_target.VolTarget(
            cfg.get("vol_target_config"), log=self.log)
        self._kelly = sizing_pkg.kelly.Kelly(cfg.get("kelly_config"),
                                             log=self.log)
        self._drawdown = sizing_pkg.drawdown_governor.DrawdownGovernor(
            cfg.get("drawdown_config"), log=self.log)

    @staticmethod
    def _limit_config(key: str, raw: Any) -> float:
        if isinstance(raw, bool):
            raise ValueError(f"{key} must be numeric")
        value = float(raw)
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"{key} must be finite and between 0 and 1")
        return value

    # ---- data ---------------------------------------------------------- #
    def _collect(self, context: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(context.get("portfolio_inputs"), dict):
            return context["portfolio_inputs"]
        if callable(self.provider):
            try:
                data = self.provider()
                if isinstance(data, dict):
                    return data
            except Exception as exc:  # noqa: BLE001
                self.log(f"provider failed ({exc!r})")
        if isinstance(self.config.get("portfolio_inputs"), dict):
            return self.config["portfolio_inputs"]
        return {}

    # ---- regime helpers ------------------------------------------------ #
    @staticmethod
    def _econ_data(context: Dict[str, Any]) -> Dict[str, Any]:
        econ = context.get("economic")
        if isinstance(econ, dict):
            return econ.get("data", {}) if isinstance(econ.get("data"), dict) else econ
        return {}

    @staticmethod
    def _market_data(context: Dict[str, Any]) -> Dict[str, Any]:
        market = context.get("market")
        if isinstance(market, dict):
            return market.get("data", {}) if isinstance(market.get("data"), dict) else market
        return {}

    def _pick_optimiser(self, econ: Dict[str, Any]) -> str:
        """Resolve the optimiser name: explicit config, else regime policy."""
        if self.optimiser and self.optimiser != "auto":
            return self.optimiser
        regime = econ.get("regime")
        policy = optim_pkg.registry.policy_for(
            regime, overrides=self.config.get("regime_policy"))
        return policy.get("optimiser", "risk_parity")

    def _target_vol_for_regime(self, econ: Dict[str, Any]) -> Optional[float]:
        """Explicit target_vol wins; else the regime policy's target."""
        if self.target_vol is not None:
            return self.target_vol
        regime = econ.get("regime")
        policy = optim_pkg.registry.policy_for(
            regime, overrides=self.config.get("regime_policy"))
        return safe_float(policy.get("target_vol"))

    # ---- 1. construction ---------------------------------------------- #
    def _construct(self, data: Dict[str, Any], market: Dict[str, Any],
                   econ: Dict[str, Any],
                   warnings: Optional[List[str]] = None) -> Dict[str, Any]:
        """Build target weights via the selected optimiser.

        Returns a dict ``{"weights", "cash", "optimiser", "diagnostics",
        "warnings"}``. Falls back to the legacy inverse-vol + leader-tilt path
        when the optimiser stack cannot run (e.g. no covariance and no vols).
        """
        universe: List[str] = list(data.get("universe") or [])
        if not universe:
            return {"weights": {}, "cash": 0.0, "optimiser": None,
                    "diagnostics": {}, "warnings": [], "views": []}

        cov = data.get("cov")
        # AUDIT (2026-10-22): ``safe_float(v) or 0.2`` and ``safe_float(v) or 1.0``
        # collapse two very different inputs to the same default:
        #   * missing vol / beta  -> documented default (0.20 / 1.00)
        #   * 0.0 (a stale or truncated sleeve) -> silently 0.20 / 1.00
        # A sleeve whose returns stopped updating shows zero variance, and the
        # optimiser then treats it as a FREE risk reduction. Zero is excluded
        # explicitly and reported so a stale feed is visible, not exploited.
        # Also: a vol above 1.0 (100% annualised) or beta above 3 is almost
        # always a units/price-scale fault, not a real estimate.
        vols_map: Dict[str, float] = {}
        betas: Dict[str, float] = {}
        invalid_inputs: List[str] = []
        for k, v in (data.get("vols") or {}).items():
            vol = safe_float(v)
            if vol is None or vol <= 0.0 or vol > 1.0:
                invalid_inputs.append(
                    f"{k}: invalid volatility {v!r}; using 0.20 (check the feed)")
                vol = 0.2
            vols_map[k] = vol
        for k, v in (data.get("betas") or {}).items():
            beta = safe_float(v)
            if beta is None or beta <= 0.0 or beta > 3.0:
                invalid_inputs.append(
                    f"{k}: invalid beta {v!r}; using 1.00 (check the feed)")
                beta = 1.0
            betas[k] = beta
        if warnings is not None:
            warnings.extend(invalid_inputs)

        constraints = {
            "min_weight": self.min_weight,
            "max_weight": self.max_weight,
            "max_gross": self.max_gross,
            "max_turnover": self.max_turnover,
            "beta_lower": self.beta_lower,
            "beta_upper": self.beta_upper,
        }
        # Per-sleeve caps from the universe map override the flat max_weight.
        uni_caps = universe_mod.resolve_universe(self.config)
        if any("cap" in (uni_caps.get(u) or {}) for u in universe):
            constraints["per_sleeve_caps"] = {
                u: safe_float((uni_caps.get(u) or {}).get("cap"))
                for u in universe
                if safe_float((uni_caps.get(u) or {}).get("cap")) is not None}

        problem: Dict[str, Any] = {
            "universe": universe,
            "cov": cov,
            "vols": vols_map,
            "betas": betas,
            "market_weights": _market_cap_weights(data, universe),
            "market_caps": data.get("market_caps") or {},
            "current_weights": data.get("current_weights") or {},
            "constraints": constraints,
            "risk_free": safe_float(self.config.get("risk_free")) or 0.0,
        }
        mu = data.get("mu") or data.get("expected_returns")
        if isinstance(mu, dict):
            problem["mu"] = mu
        if data.get("scenarios"):
            problem["scenarios"] = data["scenarios"]

        # Views (opt-in): ML / NLP / LLM -> Black-Litterman.
        views = self._collect_views(data, universe)
        if views:
            problem["views"] = views

        name = self._pick_optimiser(econ)
        result = optim_pkg.registry.optimise(name, problem, log=self.log)
        weights = result.get("weights") or {}

        # Legacy fallback: if the optimiser produced nothing usable, use the
        # original inverse-vol + leader-tilt heuristic so the engine never
        # returns an empty book when it has at least vols.
        warnings: List[str] = []
        if not weights:
            weights, warnings = self._legacy_construct(universe, vols_map, market)

        # Optional leader tilt on top of the optimiser output (small, capped).
        if weights and self.leader_tilt:
            weights = _apply_leader_tilt(weights, set(market.get("leaders") or []),
                                         self.leader_tilt)

        return {
            "weights": weights,
            "cash": round(clamp(1.0 - sum(weights.values()), 0.0, 1.0), 6),
            "optimiser": result.get("method", name),
            "diagnostics": result.get("diagnostics", {}),
            "warnings": list(result.get("warnings", [])),
            "views": views,
            "problem": problem,
        }

    def _legacy_construct(self, universe: List[str], vols_map: Dict[str, float],
                          market: Dict[str, Any]) -> tuple:
        """Original inverse-vol + leader-tilt path (kept as a safety net)."""
        from ..core.results import risk_parity_weights

        vols = [vols_map.get(n, 0.2) for n in universe]
        base = risk_parity_weights(vols) or [1.0 / len(universe)] * len(universe)
        leaders = set(market.get("leaders") or [])
        tilt = self.leader_tilt or float(self.config.get("leader_tilt", 0.25))
        tilted = [w * (1.0 + tilt) if (n in leaders and tilt) else w
                  for n, w in zip(universe, base)]
        capped = [clamp(w, self.min_weight, self.max_weight) for w in tilted]
        total = sum(capped)
        weights = {n: round(w / total, 6) for n, w in zip(universe, capped)} \
            if total > 0 else {n: w for n, w in zip(universe, base)}
        return weights, ["optimiser produced no weights; used legacy inverse-vol"]

    # ---- views --------------------------------------------------------- #
    def _collect_views(self, data: Dict[str, Any],
                       universe: List[str]) -> List[Dict[str, Any]]:
        """Assemble BL views from any enabled source (ML / NLP / LLM)."""
        views: List[Dict[str, Any]] = list(data.get("views") or [])
        if not self.views_enabled:
            return views
        cfg = self.config.get("views_config") or {}
        # ML views from a returns history.
        history = data.get("returns")
        if isinstance(history, dict) and history:
            try:
                ml = views_pkg.ml_views.views_from_history(
                    history, universe=universe,
                    horizon=int(cfg.get("horizon_days", 20)))
                views += ml.get("views", [])
            except Exception as exc:  # noqa: BLE001
                self.log(f"ml views failed ({exc!r})")
        # NLP views from headlines.
        headlines = data.get("headlines")
        if headlines:
            try:
                nlp = views_pkg.nlp_views.views_from_headlines(
                    headlines, universe=universe)
                views += nlp.get("views", [])
            except Exception as exc:  # noqa: BLE001
                self.log(f"nlp views failed ({exc!r})")
        # LLM views from a free-text blob (opt-in; no key -> no-op).
        text = data.get("text")
        if text:
            try:
                llm = views_pkg.llm_note.extract_views(text, sleeves=universe)
                views += llm.get("views", [])
            except Exception as exc:  # noqa: BLE001
                self.log(f"llm views failed ({exc!r})")
        return views_pkg.base.cluster_views(views) if views else views

    # ---- 2. risk ------------------------------------------------------- #
    def _risk(self, targets: Dict[str, float], data: Dict[str, Any]
              ) -> Dict[str, Any]:
        """Full risk block: vol, HHI, beta, tail, component contributions."""
        names = [n for n, w in targets.items() if (safe_float(w) or 0.0) > 0]
        if not names:
            return {"portfolio_vol": None, "hhi": None,
                    "dominant_contributor": None, "beta": None,
                    "contributions": {}}

        vols_map = data.get("vols") or {}
        vols = [(safe_float(vols_map.get(n)) or 0.2) for n in names]
        corr_map = data.get("corr") or {}

        def _rho(a: str, b: str) -> float:
            if a == b:
                return 1.0
            row = corr_map.get(a) or {}
            v = safe_float(row.get(b))
            if v is not None:
                return v
            rb = corr_map.get(b) or {}
            vb = safe_float(rb.get(a))
            return vb if vb is not None else 0.3

        corr = [[_rho(a, b) for b in names] for a in names]
        w = [targets[n] for n in names]

        # Prefer the full covariance matrix when the provider estimated one.
        cov = data.get("cov")
        risk_attr: Dict[str, Any] = {}
        if cov and len(cov) >= len(names):
            idx = {n: i for i, n in enumerate(data.get("universe") or [])}
            if all(a in idx for a in names):
                sub_cov = [[cov[idx[a]][idx[b]] for b in names] for a in names]
                risk_attr = risk_pkg.contribution.analyse(w, sub_cov, names,
                                                          corr=corr)
                pvol = risk_attr.get("portfolio_vol")
                contributions = risk_attr.get("contributions", {})
                dominant = risk_attr.get("dominant")
                hhi = risk_attr.get("hhi")
            else:
                cov = None
        if not risk_attr:
            from ..core.results import portfolio_vol as _pvol
            pvol = _pvol(w, vols, corr)
            wsum = sum(w)
            hhi = sum((x / wsum) ** 2 for x in w) if wsum > 0 else None
            contribs = {}
            for i, n in enumerate(names):
                c = sum(w[i] * vols[i] * w[j] * vols[j] * corr[i][j]
                        for j in range(len(names)))
                contribs[n] = abs(c)
            contributions = {k: round(v, 6) for k, v in contribs.items()}
            dominant = max(contribs, key=contribs.get) if contribs else None

        # Portfolio beta vs benchmark.
        betas = data.get("betas") or {}
        wsum = sum(w) or 1.0
        beta = None
        if betas:
            beta = sum((targets[n] / wsum) * (safe_float(betas.get(n)) or 1.0)
                       for n in names)

        risk: Dict[str, Any] = {
            "portfolio_vol": None if pvol is None else round(pvol, 6),
            "hhi": None if hhi is None else round(hhi, 6),
            "dominant_contributor": dominant,
            "beta": None if beta is None else round(beta, 4),
            "contributions": contributions,
        }
        # Merge the richer component/pcr/mctr detail when available.
        for k in ("pcr", "mctr", "variance_contributions"):
            if risk_attr.get(k):
                risk[k] = risk_attr[k]

        # Tail risk from the benchmark return series when present.
        bench_returns = _benchmark_returns(data, self.benchmark)
        if bench_returns:
            risk["tail"] = risk_pkg.tail.summarise(bench_returns)
        # Factor decomposition when a covariance is available.
        if cov and len(cov) >= len(names):
            n_obs = int(data.get("n_obs") or max(len(bench_returns), 60))
            risk["factors"] = risk_pkg.factor_model.decompose(cov, n_obs=n_obs)
        return risk

    # ---- 3. sizing ----------------------------------------------------- #
    @staticmethod
    def _economic_tilt(targets: Dict[str, float], econ: Dict[str, Any],
                       enabled: bool) -> tuple:
        """Scale the book by the Economic ``gross_exposure_multiplier``.

        Returns ``(targets, detail)``. The multiplier is clamped to a sane
        range here as well as in the economics package, because a malformed
        provider payload must never be able to lever the book up.
        """
        if not enabled:
            return targets, {"applied": False, "reason": "economic tilt disabled"}
        raw = safe_float(econ.get("gross_exposure_multiplier"))
        if raw is None:
            return targets, {
                "applied": False,
                "reason": "no economic multiplier supplied",
                "regime": econ.get("regime")}
        mult = clamp(raw, 0.0, 1.5)
        if abs(mult - 1.0) < 1e-9 or not targets:
            return targets, {"applied": False, "multiplier": round(mult, 4),
                             "reason": "multiplier is neutral",
                             "regime": econ.get("regime")}
        scaled = {k: round(v * mult, 6) for k, v in targets.items()}
        return scaled, {
            "applied": True,
            "multiplier": round(mult, 4),
            "pre_tilt_gross": round(sum(abs(v) for v in targets.values()), 6),
            "post_tilt_gross": round(sum(abs(v) for v in scaled.values()), 6),
            "regime": econ.get("regime"),
            "risk_on_score": econ.get("risk_on_score"),
        }

    def _size(self, targets: Dict[str, float], risk: Dict[str, Any],
              econ: Dict[str, Any]) -> Dict[str, Any]:
        """Scale gross via vol-target, Kelly and the drawdown governor."""
        if not self.sizing_enabled or not targets:
            return {"combined_scale": 1.0, "applied": False,
                    "reason": "sizing disabled or empty book"}
        regime_target = self._target_vol_for_regime(econ)
        vt = self._vol_target.compute(risk.get("portfolio_vol"), regime_target)
        p_win = _regime_probability(econ)
        kelly = self._kelly.compute(p_win) if self.use_kelly else {
            "gross_cap": 1.0, "reason": "kelly disabled"}
        equity = safe_float(econ.get("equity")) or safe_float(
            self.config.get("equity"))
        dd = self._drawdown.compute(equity) if self.use_drawdown else {
            "scale": 1.0, "reason": "drawdown governor disabled"}
        # The most conservative governor binds (degrade, never over-trade).
        combined = min(vt.get("scale", 1.0), kelly.get("gross_cap", 1.0),
                       dd.get("scale", 1.0))
        return {
            "applied": True,
            "combined_scale": round(combined, 6),
            "vol_target": vt,
            "kelly": kelly,
            "drawdown": dd,
            "regime_target_vol": regime_target,
            "reason": "min(vol_target, kelly, drawdown_governor)",
        }

    # ---- 4. balance ---------------------------------------------------- #
    def _balance(self, targets: Dict[str, float],
                 current: Dict[str, float]) -> List[Dict[str, Any]]:
        trades: List[Dict[str, Any]] = []
        for name in sorted(set(targets) | set(current)):
            t = safe_float(targets.get(name)) or 0.0
            c = safe_float(current.get(name)) or 0.0
            drift = t - c
            if abs(drift) >= self.rebalance_band:
                trades.append({
                    "sleeve": name,
                    "action": "BUY" if drift > 0 else "SELL",
                    "target_weight": round(t, 6),
                    "current_weight": round(c, 6),
                    "delta_weight": round(drift, 6),
                })
        return trades

    # ---- 5. hedge ------------------------------------------------------ #
    def _hedge(self, risk: Dict[str, Any]) -> Dict[str, Any]:
        beta = risk.get("beta")
        if beta is None:
            return {"needed": False, "reason": "no beta estimate",
                    "hedge_ratio": 0.0}
        excess = beta - 1.0
        if abs(excess) <= self.hedge_beta_tolerance:
            return {"needed": False, "reason": "beta within tolerance",
                    "hedge_ratio": 0.0, "beta": beta}
        ratio = clamp(abs(excess), 0.0, self.max_hedge_ratio)
        return {
            "needed": True,
            "instrument": self.benchmark,
            "side": "SELL" if excess > 0 else "BUY",
            "hedge_ratio": round(ratio, 4),
            "beta": beta,
            "reason": "residual beta outside tolerance",
        }

    def _apply_exposure_limits(self, targets: Dict[str, float],
                               data: Dict[str, Any],
                               warnings: List[str]) -> Dict[str, float]:
        """Enforce the configured cash reserve and known technology-sector cap."""
        adjusted = dict(targets)
        sector_by_symbol = data.get("sector_by_symbol") or {}
        if not isinstance(sector_by_symbol, dict):
            sector_by_symbol = {}

        def _is_technology(symbol: str) -> bool:
            sector = str(sector_by_symbol.get(symbol, "")).strip().lower()
            words = set(sector.replace("/", " ").replace("-", " ").split())
            return (
                "technology" in words
                or "software" in words
                or "tech" in words
                or {"it", "services"}.issubset(words)
            )

        tech_symbols = [symbol for symbol in adjusted if _is_technology(symbol)]
        if tech_symbols:
            tech_gross = sum(abs(adjusted[symbol]) for symbol in tech_symbols)
            if tech_gross > self.max_tech_sector_exposure and tech_gross > 0:
                scale = self.max_tech_sector_exposure / tech_gross
                for symbol in tech_symbols:
                    adjusted[symbol] *= scale
        elif adjusted and any(symbol not in sector_by_symbol for symbol in adjusted):
            warnings.append(
                "technology-sector limit not verifiable: sector_by_symbol data missing")

        gross = sum(abs(weight) for weight in adjusted.values())
        gross_cap = min(self.max_gross, 1.0 - self.min_cash_buffer_pct)
        if gross > gross_cap and gross > 0:
            scale = gross_cap / gross
            adjusted = {symbol: weight * scale
                        for symbol, weight in adjusted.items()}
        return {symbol: round(weight, 6)
                for symbol, weight in adjusted.items()}

    # ---- main ---------------------------------------------------------- #
    def _run(self, context: Dict[str, Any]) -> EngineResult:
        warnings: List[str] = []
        data = self._collect(context)
        econ = self._econ_data(context)
        market = self._market_data(context)

        if not data:
            return EngineResult(
                engine=self.name, ok=True,
                data={"target_weights": {}, "rebalance_trades": [],
                      "risk": {}, "hedge": {"needed": False},
                      "max_tech_sector_exposure": self.max_tech_sector_exposure,
                      "min_cash_buffer_pct": self.min_cash_buffer_pct,
                      "rebalance_allowed": False,
                      "rationale": "Portfolio inputs unavailable; no rebalance proposed."},
                warnings=["no portfolio inputs supplied; empty target book"],
            )

        built = self._construct(data, market, econ, warnings)
        warnings.extend(built.get("warnings", []))
        targets = self._apply_exposure_limits(
            built["weights"], data, warnings)
        # Economic tilt BEFORE risk/sizing: the multiplier is a gross-exposure
        # decision, so vol-target and the drawdown governor must see (and may
        # further reduce, never increase) the already-tilted book.
        targets, tilt = self._economic_tilt(
            targets, econ, self.consume_gross_multiplier)
        # Audit (2026-10-22): the tilt ran AFTER the cash-buffer/gross cap, so an
        # up-tilt (multiplier 1.3, an allowed RISK_ON value) pushed gross back
        # above max_gross / (1 - min_cash_buffer_pct) - verified: gross went from
        # 0.85 to 1.105 against a 0.85 cap. The hard caps are absolute, so they
        # are re-applied AFTER the tilt; the tilt may only reduce headroom that
        # the caps allow, never breach them.
        if tilt.get("applied"):
            targets = self._apply_exposure_limits(targets, data, warnings)
        if tilt.get("applied"):
            warnings.append(
                f"gross scaled by economic multiplier {tilt['multiplier']} "
                f"(regime={tilt.get('regime')})")
        current = {k: (safe_float(v) or 0.0)
                   for k, v in (data.get("current_weights") or {}).items()}

        # Risk BEFORE sizing (sizing needs the forecast vol).
        risk = self._risk(targets, data)

        # Sizing: scale the whole book by the combined governor.
        sizing = self._size(targets, risk, econ)
        scale = sizing.get("combined_scale", 1.0)
        if sizing.get("applied") and abs(scale - 1.0) > 1e-9:
            targets = {k: round(v * scale, 6) for k, v in targets.items()}
            # Recompute risk on the sized book so the reported vol is the real one.
            risk = self._risk(targets, data)
            risk["gross_scale"] = round(scale, 6)

        cash = round(clamp(1.0 - sum(targets.values()), 0.0, 1.0), 6)

        # Cost-aware rebalance decision.
        banded = optim_pkg.cost_aware.apply_band(
            targets, current, band_pct=self.cost_band_pct,
            costs_config=self.config.get("costs"))
        effective_targets = banded.get("effective_target", targets)
        trades = self._balance(effective_targets, current)
        risk_data = context.get("risk")
        if isinstance(risk_data, dict) and isinstance(risk_data.get("data"), dict):
            risk_data = risk_data["data"]
        risk_veto = bool(
            isinstance(risk_data, dict)
            and risk_data.get("risk_veto_active"))
        risk_veto = risk_veto or market.get("market_volatility_state") == "High_Panic"
        rebalance_allowed = not risk_veto
        if not rebalance_allowed:
            trades = []
            warnings.append("rebalance suppressed by risk veto or high-panic market state")
        cost = banded.get("cost", {})

        hedge = self._hedge(risk)

        # Attribution (drift + perf) when there is a history / benchmark.
        attribution = self._attribution(targets, current, data, risk)

        result_data: Dict[str, Any] = {
            "target_weights": targets,
            "effective_targets": effective_targets,
            "rebalance_trades": trades,
            "risk": risk,
            "hedge": hedge,
            "capital": safe_float(data.get("capital")),
            "cash_weight": cash,
            # NEW fields (superset; downstream may ignore them).
            "optimiser": built.get("optimiser"),
            "gross": round(sum(targets.values()), 6),
            "constraints": built.get("diagnostics", {}),
            "sizing": sizing,
            "economic_tilt": tilt,
            "cost": cost,
            "held_in_band": banded.get("held", []),
            "views": built.get("views", []),
            "attribution": attribution,
            "regime": econ.get("regime"),
            "max_tech_sector_exposure": self.max_tech_sector_exposure,
            "min_cash_buffer_pct": self.min_cash_buffer_pct,
            "rebalance_allowed": rebalance_allowed,
            "rationale": (
                "Risk veto or high-panic regime blocks rebalance."
                if not rebalance_allowed else
                "Technology exposure capped when sector data is available; minimum cash buffer enforced."),
        }

        self.log(f"optimiser={built.get('optimiser')} gross="
                 f"{result_data['gross']:.3f} trades={len(trades)} "
                 f"vol={risk.get('portfolio_vol')} scale={scale:.2f} "
                 f"hedge={hedge.get('needed')}")

        if self.persist:
            self._persist(result_data, current)

        return EngineResult(engine=self.name, ok=True, data=result_data,
                            warnings=warnings)

    # ---- attribution --------------------------------------------------- #
    def _attribution(self, targets: Dict[str, float],
                     current: Dict[str, float], data: Dict[str, Any],
                     risk: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        try:
            out["drift"] = attr.drift.drift(targets, current)
            out["tracking"] = attr.drift.tracking_difference(targets, current)
        except Exception as exc:  # noqa: BLE001
            self.log(f"drift attribution failed ({exc!r})")
        bench = data.get("benchmark_weights")
        sleeve_ret = data.get("sleeve_returns")
        bench_ret = data.get("benchmark_sleeve_returns")
        if isinstance(bench, dict) and isinstance(sleeve_ret, dict) \
                and isinstance(bench_ret, dict):
            try:
                out["brinson"] = attr.brinson.brinson_attribution(
                    targets, bench, sleeve_ret, bench_ret)
            except Exception as exc:  # noqa: BLE001
                self.log(f"brinson attribution failed ({exc!r})")
        return out

    # ---- persistence --------------------------------------------------- #
    def _persist(self, result_data: Dict[str, Any],
                 current: Dict[str, float]) -> None:
        try:
            from . import portfolio_state
            portfolio_state.record({
                "regime": result_data.get("regime"),
                "optimiser": result_data.get("optimiser"),
                "target_weights": result_data.get("target_weights"),
                "actual_weights": current,
                "gross": result_data.get("gross"),
                "cash_weight": result_data.get("cash_weight"),
                "risk": result_data.get("risk"),
                "sizing": result_data.get("sizing"),
                "cost": result_data.get("cost"),
            })
        except Exception as exc:  # noqa: BLE001
            self.log(f"persist failed ({exc!r})")


# --------------------------------------------------------------------------- #
# Module helpers
# --------------------------------------------------------------------------- #

def _market_cap_weights(data: Dict[str, Any],
                        universe: List[str]) -> Dict[str, float]:
    """Resolve market-cap weights for the BL prior (config > data > equal)."""
    for key in ("market_weights", "market_caps", "caps"):
        mw = data.get(key)
        if isinstance(mw, dict) and mw:
            return {u: (safe_float(mw.get(u)) or 0.0) for u in universe}
    return {u: 1.0 for u in universe}


def _apply_leader_tilt(weights: Dict[str, float], leaders: set,
                       tilt: float) -> Dict[str, float]:
    """Small, capped tilt toward market leaders, then renormalise."""
    if not leaders or not tilt:
        return weights
    tilted = {k: (v * (1.0 + tilt) if k in leaders else v)
              for k, v in weights.items()}
    total = sum(tilted.values())
    if total <= 0:
        return weights
    return {k: round(v / total, 6) for k, v in tilted.items()}


def _regime_probability(econ: Dict[str, Any]) -> Optional[float]:
    """Extract P(risk-on) for Kelly, from the economic payload if present."""
    p = safe_float(econ.get("risk_on_probability"))
    if p is not None:
        return p
    # Map the score in [-1,1] to a probability via a logistic transform.
    score = safe_float(econ.get("risk_on_score"))
    if score is None:
        return None
    import math
    return 1.0 / (1.0 + math.exp(-2.0 * score))


def _benchmark_returns(data: Dict[str, Any], benchmark: str) -> List[float]:
    """The benchmark's return series from the provider's returns panel."""
    returns = data.get("returns")
    if isinstance(returns, dict):
        series = returns.get(benchmark)
        if isinstance(series, list):
            return [safe_float(x) for x in series if safe_float(x) is not None]
    bench = data.get("benchmark_returns")
    if isinstance(bench, list):
        return [safe_float(x) for x in bench if safe_float(x) is not None]
    return []


__all__ = ["PortfolioEngine"]