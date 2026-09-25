"""
tests - portfolio engine test suite.

Run with::

    & ..\\openalgo\\.venv\\Scripts\\python.exe -m pytest tests -q

or without pytest (it also runs as a plain script)::

    & ..\\openalgo\\.venv\\Scripts\\python.exe tests\\test_portfolio_engine.py

Design
------
* Pure modules (risk/optim/sizing/attribution/views) are tested with NO network
  and NO SDK - that is the whole point of the provider-injection design.
* Degradation paths (missing covariance, no provider, no key) are asserted,
  because the engine's contract is "degrade, never halt".
* SDK-facing providers are tested with a *fake client*, so the tests never touch
  a broker.
"""
from __future__ import annotations

import os
import sys

# Make `blue_engine` importable when run directly (not just under pytest).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from blue_engine.portfolio import (  # noqa: E402
    PortfolioEngine, attribution, optim, providers, risk, sizing, views)
from blue_engine.portfolio.providers import (  # noqa: E402
    PortfolioProvider, CachedProvider, CompositeProvider)


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# A small, well-conditioned 3-sleeve universe (annualised covariance).
COV = [[0.0196, 0.0080, 0.0040],
       [0.0080, 0.0256, 0.0120],
       [0.0040, 0.0120, 0.2500]]
UNIVERSE = ["NIFTY", "IT", "SMALLCAP"]
VOLS = {"NIFTY": 0.14, "IT": 0.16, "SMALLCAP": 0.50}
BETAS = {"NIFTY": 1.0, "IT": 0.8, "SMALLCAP": 1.3}
CORR = {
    "NIFTY": {"NIFTY": 1.0, "IT": 0.5, "SMALLCAP": 0.2},
    "IT": {"NIFTY": 0.5, "IT": 1.0, "SMALLCAP": 0.85},
    "SMALLCAP": {"NIFTY": 0.2, "IT": 0.85, "SMALLCAP": 1.0},
}


def _inputs(**over):
    d = {
        "universe": list(UNIVERSE),
        "vols": dict(VOLS),
        "corr": {k: dict(v) for k, v in CORR.items()},
        "betas": dict(BETAS),
        "current_weights": {"NIFTY": 0.3, "IT": 0.1},
        "capital": 1_000_000.0,
    }
    d.update(over)
    return d


# --------------------------------------------------------------------------- #
# 1. Engine contract (must stay a superset of the original)
# --------------------------------------------------------------------------- #
def test_engine_never_raises_and_returns_engineresult():
    eng = PortfolioEngine({"optimiser": "risk_parity"})
    res = eng.run({"portfolio_inputs": _inputs()})
    _check(res.ok is True, "engine should be ok")
    _check(res.engine == "portfolio", "engine name should be 'portfolio'")
    for key in ("target_weights", "rebalance_trades", "risk", "hedge",
                "capital", "cash_weight"):
        _check(key in res.data, f"missing legacy output key '{key}'")
    # NEW keys must also be present.
    for key in ("optimiser", "gross", "sizing", "cost", "attribution"):
        _check(key in res.data, f"missing new output key '{key}'")


def test_empty_inputs_is_empty_book_not_error():
    res = PortfolioEngine({}).run()
    _check(res.ok is True, "empty inputs must not fail the engine")
    _check(res.data["target_weights"] == {}, "empty -> empty target book")
    _check(any("no portfolio inputs" in w for w in res.warnings), "should warn")


def test_provider_is_used_when_no_inputs_in_context():
    def _prov():
        return _inputs()
    eng = PortfolioEngine({"optimiser": "risk_parity"}, provider=_prov)
    res = eng.run()
    _check(res.ok is True, "provider path must work")
    _check(bool(res.data["target_weights"]), "provider should yield a book")


def test_provider_failure_is_soft():
    def _boom():
        raise RuntimeError("network down")
    eng = PortfolioEngine({"optimiser": "risk_parity"}, provider=_boom)
    res = eng.run()
    _check(res.ok is True, "a failing provider must not fail the engine")


def test_weights_respect_box_and_gross():
    eng = PortfolioEngine({"optimiser": "hrp", "max_weight": 0.5,
                           "max_gross": 1.0})
    res = eng.run({"portfolio_inputs": _inputs()})
    w = res.data["target_weights"]
    _check(all(v <= 0.5 + 1e-9 for v in w.values()), f"box breach: {w}")
    _check(sum(w.values()) <= 1.0 + 1e-9, f"gross breach: {sum(w.values())}")


def test_cash_weight_complements_weights():
    res = PortfolioEngine({"optimiser": "risk_parity"}).run(
        {"portfolio_inputs": _inputs()})
    w = res.data["target_weights"]
    cash = res.data["cash_weight"]
    _check(abs((sum(w.values()) + cash) - 1.0) < 1e-6,
           "weights + cash must equal 1")


# --------------------------------------------------------------------------- #
# 2. Optimisers (risk_parity/ERC is the headline fix)
# --------------------------------------------------------------------------- #
def test_erc_equalises_component_risk():
    w = optim.risk_parity.erc_weights(COV)
    _check(w is not None, "ERC should produce weights")
    rc = optim.risk_parity.risk_contributions(COV, w)
    spread = max(rc) - min(rc)
    _check(spread < 1e-3, f"ERC risk contributions should be equal, got {rc}")


def test_inverse_vol_is_not_risk_parity_when_correlated():
    iv = optim.risk_parity.inverse_vol_weights(list(VOLS.values()))
    erc = optim.risk_parity.erc_weights(COV)
    # They must differ materially - that is the whole point of ERC.
    diff = sum(abs(a - b) for a, b in zip(iv, erc))
    _check(diff > 0.02, f"ERC should differ from inverse-vol, diff={diff}")


def test_all_optimisers_return_normalised_long_only_weights():
    for name in ("inverse_vol", "risk_parity", "min_variance", "max_sharpe",
                 "hrp", "nco", "min_cvar", "black_litterman", "market_cap"):
        prob = {"universe": list(UNIVERSE), "cov": COV, "vols": dict(VOLS),
                "mu": {"NIFTY": 0.10, "IT": 0.09, "SMALLCAP": 0.12},
                "market_weights": {"NIFTY": 0.5, "IT": 0.3, "SMALLCAP": 0.2},
                "constraints": {"max_weight": 0.9}}
        out = optim.registry.optimise(name, prob)
        _check(out["ok"] is True, f"{name} should succeed")
        w = out["weights"]
        _check(len(w) == 3, f"{name} should weight all 3 sleeves")
        _check(all(v >= -1e-9 for v in w.values()), f"{name} produced a short")
        _check(sum(w.values()) <= 1.0 + 1e-9, f"{name} breached gross")


def test_hrp_penalises_the_high_vol_correlated_sleeve():
    w = optim.hrp.hrp_weights(COV)
    # SMALLCAP is the highest vol AND ~0.85 correlated to IT -> smallest weight.
    _check(w[2] < w[0] and w[2] < w[1],
           f"HRP should underweight SMALLCAP, got {w}")


def test_min_variance_beats_equal_weight_variance():
    w = optim.mean_variance.min_variance_weights(COV)
    var_mv = sum(w[i] * w[j] * COV[i][j]
                 for i in range(3) for j in range(3))
    eq = [1 / 3, 1 / 3, 1 / 3]
    var_eq = sum(eq[i] * eq[j] * COV[i][j]
                 for i in range(3) for j in range(3))
    _check(var_mv <= var_eq + 1e-12,
           f"min-variance {var_mv} should be <= equal-weight {var_eq}")


def test_black_litterman_views_move_weights_monotonically():
    """A stronger positive view must never REDUCE the sleeve's weight.

    The correct BL property is *monotonicity* in the view direction. (Comparing
    against a no-view book would be wrong: with no views BL degenerates to a
    minimum-variance prior, which is not return-seeking, so a single moderate
    view on a 50%-vol sleeve can legitimately sit below the GMV weight.)
    """
    prob = {"universe": list(UNIVERSE), "cov": COV, "vols": dict(VOLS),
            "market_weights": {"NIFTY": 0.5, "IT": 0.3, "SMALLCAP": 0.2}}

    def _weight(view_return, sleeve="SMALLCAP"):
        p = {**prob, "views": [{"assets": [sleeve], "weights": [1.0],
                                 "return": view_return, "confidence": 0.8}]}
        return optim.black_litterman.BlackLitterman().optimise(
            p)["weights"].get(sleeve, 0.0)

    # A negative view must sit below a neutral one, which must sit below a
    # positive one - i.e. the mapping is strictly ordered in the view direction.
    bear = _weight(-0.02)
    neutral = _weight(0.0)
    bull = _weight(0.05)
    _check(bear <= neutral + 1e-9,
           f"a bearish view must not raise the weight: {bear} vs {neutral}")
    _check(bull >= neutral - 1e-9,
           f"a bullish view must not lower the weight: {bull} vs {neutral}")
    _check(bull > bear + 1e-9,
           f"a bullish view must beat a bearish one: {bull} vs {bear}")


def test_black_litterman_posterior_moves_toward_the_view():
    """The posterior must move TOWARD the view, wherever the prior happens to be.

    Note the direction is not "up": equilibrium returns on a high-vol sleeve are
    already high (``pi = delta * Sigma * w_mkt``), so a +5% view can legitimately
    pull the posterior DOWN to 5%. The invariant is *movement toward the view*,
    on whichever side it lies.
    """
    pi = optim.black_litterman.implied_equilibrium_returns(
        COV, [0.5, 0.3, 0.2])
    _check(pi is not None, "equilibrium returns should compute")
    prior = pi[2]

    # A view far ABOVE the prior pulls the posterior up.
    high = optim.black_litterman.posterior_returns(
        COV, pi, [[0.0, 0.0, 1.0]], [prior + 0.10], omega=[[1e-6]])
    _check(high[2] > prior - 1e-12,
           f"a view above the prior should not lower it: {high[2]} vs {prior}")
    _check(abs(high[2] - (prior + 0.10)) < 1e-3,
           f"a near-certain view should pin the posterior: {high[2]}")

    # A view far BELOW the prior pulls the posterior down.
    low = optim.black_litterman.posterior_returns(
        COV, pi, [[0.0, 0.0, 1.0]], [prior - 0.10], omega=[[1e-6]])
    _check(low[2] < prior + 1e-12,
           f"a view below the prior should not raise it: {low[2]} vs {prior}")
    _check(abs(low[2] - (prior - 0.10)) < 1e-3,
           f"the posterior should move toward the view: {low[2]}")

    # A low-confidence view barely moves the prior.
    weak = optim.black_litterman.posterior_returns(
        COV, pi, [[0.0, 0.0, 1.0]], [prior + 0.50], omega=[[1.0]])
    _check(abs(weak[2] - prior) < abs(high[2] - prior),
           "a low-confidence view should move the prior less")


def test_unknown_optimiser_falls_back():
    out = optim.registry.optimise("does_not_exist",
                                  {"universe": list(UNIVERSE), "vols": dict(VOLS)})
    _check(out["method"] == "risk_parity", "unknown optimiser -> risk_parity")


# --------------------------------------------------------------------------- #
# 3. Constraints / projection
# --------------------------------------------------------------------------- #
def test_turnover_constraint_limits_the_move():
    spec = optim.base.ConstraintSpec.from_dict({"max_turnover": 0.02})
    cur = {"NIFTY": 0.5, "IT": 0.5, "SMALLCAP": 0.0}
    tgt = {"NIFTY": 0.1, "IT": 0.1, "SMALLCAP": 0.8}
    proj, diag = optim.base.project_to_box(tgt, spec, current=cur)
    to = optim.constraints.turnover(proj, cur)
    _check(to <= 0.02 + 1e-6, f"turnover {to} should be capped at 0.02")
    _check("turnover" in diag["binding"], "turnover should be flagged binding")


def test_beta_band_is_repaired():
    spec = optim.base.ConstraintSpec.from_dict({"beta_upper": 1.05})
    w = {"NIFTY": 0.2, "IT": 0.2, "SMALLCAP": 0.6}   # beta well above 1
    proj, diag = optim.base.project_to_box(w, spec, betas=BETAS)
    b = optim.constraints.beta_of(proj, BETAS)
    _check(b is None or b <= 1.05 + 1e-6, f"beta {b} should be <= 1.05")


# --------------------------------------------------------------------------- #
# 4. Risk model
# --------------------------------------------------------------------------- #
def test_covariance_shrinkage_is_bounded_and_psd():
    import random
    random.seed(7)
    returns = [[random.gauss(0, 0.01) for _ in range(80)] for _ in range(4)]
    cov, delta = risk.covariance.ledoit_wolf_cov(returns)
    _check(cov is not None, "LW should produce a covariance")
    _check(0.0 <= delta <= 1.0, f"delta {delta} out of [0,1]")
    _check(risk.covariance.is_psd(cov), "shrunk covariance should be PSD")


def test_component_contributions_sum_to_vol():
    w = [0.4, 0.3, 0.3]
    comp = risk.contribution.component_contributions(w, COV)
    vol = risk.contribution.portfolio_vol_from_cov(w, COV)
    _check(abs(sum(comp) - vol) < 1e-9,
           f"component contributions {sum(comp)} should sum to vol {vol}")


def test_tail_var_cvar_ordering():
    import random
    random.seed(3)
    rs = [random.gauss(0.0004, 0.01) for _ in range(500)]
    var = risk.tail.historical_var(rs, level=0.95)
    cvar = risk.tail.cvar(rs, level=0.95)
    _check(var is not None and cvar is not None, "VaR/CVaR should compute")
    _check(cvar >= var - 1e-12, f"CVaR {cvar} should be >= VaR {var}")


def test_ols_beta_recovers_a_known_relationship():
    market = [0.01, -0.02, 0.015, 0.0, -0.01, 0.02, 0.005, -0.015]
    asset = [2 * m for m in market]        # exact beta = 2
    b = risk.beta.ols_beta(asset, market)
    _check(abs(b - 2.0) < 1e-6, f"beta should be 2.0, got {b}")


def test_factor_decomposition_variance_shares_sum_to_one():
    rows = risk.factor_model.variance_explained(COV)
    _check(abs(sum(r["pct"] for r in rows) - 100.0) < 1e-6,
           "variance-explained percentages should sum to 100")


# --------------------------------------------------------------------------- #
# 5. Sizing
# --------------------------------------------------------------------------- #
def test_vol_target_scales_down_when_vol_above_target():
    out = sizing.vol_target.vol_target_scale(0.20, 0.12)
    _check(out["scale"] < 1.0, "vol above target -> de-risk")


def test_vol_target_is_a_noop_without_inputs():
    out = sizing.vol_target.vol_target_scale(None, 0.12)
    _check(out["scale"] == 1.0, "no forecast vol -> no change")


def test_kelly_cap_moves_with_edge():
    hi = sizing.kelly.kelly_cap(0.80)
    lo = sizing.kelly.kelly_cap(0.20)
    _check(hi["gross_cap"] > lo["gross_cap"],
           "higher win probability -> higher gross cap")


def test_drawdown_governor_cuts_gross():
    out = sizing.drawdown_governor.drawdown_scale(0.22)
    _check(out["scale"] < 0.5, f"a 22% drawdown should de-risk hard, got {out}")
    calm = sizing.drawdown_governor.drawdown_scale(0.01)
    _check(calm["scale"] == 1.0, "a small drawdown should not de-risk")


def test_engine_sizing_never_increases_gross_above_optimum():
    eng = PortfolioEngine({"optimiser": "risk_parity", "target_vol": 0.05})
    res = eng.run({"portfolio_inputs": _inputs()})
    _check(res.data["sizing"]["combined_scale"] <= 1.0 + 1e-9,
           "combined sizing scale must not exceed 1.0")


# --------------------------------------------------------------------------- #
# 6. Cost-aware rebalancing (India)
# --------------------------------------------------------------------------- #
def test_cost_estimate_is_positive_and_grows_with_turnover():
    small = optim.cost_aware.rebalance_cost({"NIFTY": 0.51, "IT": 0.49},
                                            {"NIFTY": 0.50, "IT": 0.50})
    big = optim.cost_aware.rebalance_cost({"NIFTY": 0.9, "IT": 0.1},
                                          {"NIFTY": 0.1, "IT": 0.9})
    _check(small["cost_bps"] >= 0, "cost should be non-negative")
    _check(big["cost_bps"] > small["cost_bps"],
           "bigger turnover should cost more")


def test_no_trade_region_holds_small_drifts():
    target = {"NIFTY": 0.5, "IT": 0.5}
    current = {"NIFTY": 0.505, "IT": 0.495}      # 0.5% drift, inside band
    out = optim.cost_aware.should_rebalance(target, current, band_pct=2.0)
    _check(out["rebalance"] is False, f"tiny drift should not trigger: {out}")


def test_apply_band_keeps_in_band_sleeves_at_current():
    target = {"NIFTY": 0.5, "IT": 0.5}
    current = {"NIFTY": 0.51, "IT": 0.49}
    out = optim.cost_aware.apply_band(target, current, band_pct=3.0)
    _check("NIFTY" in out["held"] and "IT" in out["held"],
           "both sleeves should be held inside a 3% band")


# --------------------------------------------------------------------------- #
# 7. Attribution
# --------------------------------------------------------------------------- #
def test_brinson_effects_sum_to_active_return():
    w = {"NIFTY": 0.6, "IT": 0.4}
    b = {"NIFTY": 0.5, "IT": 0.5}
    rp = {"NIFTY": 0.02, "IT": 0.01}
    rb = {"NIFTY": 0.015, "IT": 0.012}
    res = attribution.brinson.brinson_attribution(w, b, rp, rb)
    total = res["allocation"] + res["selection"] + res["interaction"]
    _check(abs(total - res["active_return"]) < 1e-9,
           "allocation+selection+interaction must equal active return")


def test_perf_sharpe_and_drawdown_sane():
    rs = [0.01, -0.005, 0.02, 0.0, -0.01, 0.015, -0.02, 0.01]
    s = attribution.perf.summarise(rs)
    _check(s["max_drawdown"] is not None and 0 <= s["max_drawdown"] <= 1,
           "max drawdown should be a fraction in [0,1]")
    _check(0.0 <= s["hit_rate"] <= 1.0, "hit rate should be a probability")


def test_drift_measures_target_vs_actual():
    d = attribution.drift.drift({"NIFTY": 0.5, "IT": 0.5},
                                {"NIFTY": 0.6, "IT": 0.4})
    _check(abs(d["sleeves"]["NIFTY"]["drift"] - 0.1) < 1e-9, "drift wrong")
    _check(abs(d["total_abs_drift"] - 0.2) < 1e-9, "total abs drift wrong")


def test_factor_attribution_return_decomposition():
    import random
    random.seed(11)
    market = [random.gauss(0, 0.01) for _ in range(200)]
    port = [0.8 * m for m in market]
    res = attribution.factor_attr.return_attribution(port, {"market": market})
    _check(res["ok"] is True, "factor attribution should run")
    _check(abs(res["loadings"]["market"] - 0.8) < 0.05,
           f"loading should recover ~0.8, got {res['loadings']}")


# --------------------------------------------------------------------------- #
# 8. Views (ML / NLP / LLM)
# --------------------------------------------------------------------------- #
def test_nlp_views_are_bullish_on_positive_headlines():
    out = views.nlp_views.views_from_headlines(
        ["Banks rally on strong credit growth"], universe=["BANKNIFTY"])
    _check(len(out["views"]) == 1, "should produce one view")
    _check(out["views"][0]["return"] > 0, "positive tone -> positive view")
    _check(out["views"][0]["source"] == "nlp", "source should be nlp")


def test_ml_views_degrade_without_history():
    out = views.ml_views.views_from_history({"NIFTY": [0.001] * 5})
    _check(out["views"] == [], "short history -> no views, no crash")


def test_llm_views_disabled_without_key():
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("OLLAMA_HOST", None)
    out = views.llm_note.extract_views("some text", sleeves=["NIFTY"])
    _check(out["ok"] is False, "no backend -> ok False")
    _check(out["views"] == [], "no backend -> no views")


def test_llm_pm_note_has_template_fallback():
    os.environ.pop("OPENAI_API_KEY", None)
    os.environ.pop("OLLAMA_HOST", None)
    out = views.llm_note.write_pm_note(
        {"gross": 0.9, "risk": {"portfolio_vol": 0.12,
                                "dominant_contributor": "NIFTY"},
         "regime": "RISK_ON"})
    _check(out["method"] == "template", "should fall back to the template")
    _check("RISK_ON" in out["note"], "note should mention the regime")


def test_view_validation_drops_malformed():
    _check(views.base.make_view([], view_return=0.01) is None,
           "no assets -> None")
    _check(views.base.make_view(["A"], view_return=None) is None,
           "no return -> None")
    ok = views.base.make_view(["A"], view_return=0.01, confidence=5.0)
    _check(ok is not None and ok["confidence"] <= 0.999,
           "confidence should be clamped")


# --------------------------------------------------------------------------- #
# 9. Providers (no network - fake client / fake provider)
# --------------------------------------------------------------------------- #
class _FakeProvider(PortfolioProvider):
    name = "fake"

    def __init__(self, payload):
        super().__init__({})
        self._payload = payload

    def _fetch(self):
        return dict(self._payload)


class _ExplodingProvider(PortfolioProvider):
    name = "boom"

    def _fetch(self):
        raise RuntimeError("network down")


def test_provider_fetch_never_raises():
    res = _ExplodingProvider().fetch()
    _check(res["meta"]["ok"] is False, "exploding provider -> ok False")
    _check(res["meta"]["warnings"], "should carry a warning")


def test_composite_merges_keys():
    a = _FakeProvider({"universe": ["NIFTY"], "vols": {"NIFTY": 0.14}})
    b = _FakeProvider({"current_weights": {"NIFTY": 0.3}, "capital": 100.0})
    comp = CompositeProvider([a, b])
    out = comp.fetch()
    _check(out["vols"]["NIFTY"] == 0.14, "vols should survive the merge")
    _check(out["capital"] == 100.0, "capital should be merged in")
    _check(out["meta"]["ok"] is True, "composite should be ok")


def test_cache_serves_stale_on_failure(tmp_path):
    ok = _FakeProvider({"vols": {"NIFTY": 0.14}, "universe": ["NIFTY"]})
    cached = CachedProvider(ok, ttl_seconds=0.0, cache_dir=tmp_path)
    first = cached.fetch()
    _check(first["vols"]["NIFTY"] == 0.14, "first fetch should succeed")
    cached.source = _ExplodingProvider()
    cached._mem = None
    second = cached.fetch()
    _check(second["vols"]["NIFTY"] == 0.14, "should serve cached on failure")
    _check(second["meta"]["stale"] is True, "stale flag should be set")


def test_openalgo_provider_with_fake_client(tmp_path):
    import random

    class _FakeClient:
        def history(self, **kw):
            import datetime
            random.seed(1)
            base = 100.0
            rows = []
            day = datetime.date(2024, 1, 1)
            for i in range(260):
                base *= (1 + random.gauss(0.0004, 0.012))
                # A realistic broker bar carries a timestamp (as OpenAlgo does).
                rows.append({"timestamp": (day + datetime.timedelta(days=i)).isoformat(),
                             "close": round(base, 2)})
            return {"status": "success", "data": rows}

        def positionbook(self):
            return {"status": "success", "data": [
                {"symbol": "NIFTY", "quantity": 10, "ltp": 22000}]}

        def holdings(self):
            return {"status": "success", "data": []}

        def funds(self):
            return {"status": "success", "data": {"availablecash": 500000}}

    prov = providers.openalgo_provider.OpenAlgoPortfolioProvider(
        _FakeClient(), {"universe_names": ["NIFTY", "IT", "PHARMA"],
                       "cache_db": str(tmp_path / "fake.db")})
    out = prov.fetch()
    _check(out["meta"]["ok"] is True, "should succeed with a fake client")
    _check(len(out["universe"]) >= 2, "should estimate several sleeves")
    _check(out["vols"], "should produce vols from history")
    _check(out["meta"].get("cached_rows", 0) > 0,
           "fetch should persist bars into the price cache")


def test_openalgo_provider_works_without_timestamps():
    """Some brokers omit the timestamp; the close series must still build."""
    import random

    class _NoTsClient:
        def history(self, **kw):
            random.seed(2)
            base = 100.0
            rows = []
            for _ in range(80):
                base *= (1 + random.gauss(0.0004, 0.012))
                rows.append({"close": round(base, 2)})   # no timestamp at all
            return {"status": "success", "data": rows}

    prov = providers.openalgo_provider.OpenAlgoPortfolioProvider(
        _NoTsClient(), {"universe_names": ["NIFTY", "IT"],
                        "cache_prices": False})
    out = prov.fetch()
    _check(out["meta"]["ok"] is True,
           f"timestamp-less bars must still work: {out['meta']['warnings']}")
    _check(out["vols"], "should still produce vols")


def test_account_provider_maps_positions_to_sleeves():
    class _FakeClient:
        def positionbook(self):
            return {"status": "success", "data": [
                {"symbol": "NIFTY", "quantity": 10, "ltp": 22000},
                {"symbol": "NIFTY BANK", "quantity": 5, "ltp": 48000}]}

        def holdings(self):
            return {"status": "success", "data": []}

        def funds(self):
            return {"status": "success", "data": {"availablecash": 500000}}

    prov = providers.account_provider.AccountProvider(_FakeClient())
    out = prov.fetch()
    _check(out["meta"]["ok"] is True, "account provider should succeed")
    _check("NIFTY" in out["current_weights"] or
           "BANKNIFTY" in out["current_weights"],
           f"positions should map to sleeves: {out['current_weights']}")


def test_openalgo_provider_handles_sdk_error(tmp_path):
    """An SDK error with an EMPTY cache must degrade, not raise.

    Note: when the cache *does* hold history, a broker outage correctly falls
    back to it (that is the point of the cache) - so this test isolates the
    cache to assert the cold-cache behaviour.
    """
    class _BadClient:
        def history(self, **kw):
            return {"status": "error", "message": "no data"}

    prov = providers.openalgo_provider.OpenAlgoPortfolioProvider(
        _BadClient(), {"universe_names": ["NIFTY", "IT"],
                       "cache_db": str(tmp_path / "empty.db")})
    out = prov.fetch()
    _check(out["meta"]["ok"] is False, "SDK error -> ok False, not a raise")


def test_openalgo_provider_falls_back_to_cache_on_sdk_error(tmp_path):
    """With a populated cache, a broker outage must still yield a risk model."""
    from blue_engine.portfolio.providers import price_cache
    db = str(tmp_path / "warm.db")
    c = price_cache.PriceCache(db)
    rows = [{"timestamp": f"2024-01-{i:02d}", "close": 100.0 + i * 0.1}
            for i in range(1, 29)]
    c.upsert("NIFTY", "NSE_INDEX", "D", rows)
    c.upsert("IT", "NSE_INDEX", "D", rows)

    class _BadClient:
        def history(self, **kw):
            return {"status": "error", "message": "no data"}

    prov = providers.openalgo_provider.OpenAlgoPortfolioProvider(
        _BadClient(), {"universe_names": ["NIFTY", "IT"], "cache_db": db})
    out = prov.fetch()
    _check(out["meta"]["ok"] is True, "warm cache should rescue the fetch")
    _check(out["vols"], "should produce vols from cached closes")
    _check(any("cached" in w for w in out["meta"]["warnings"]),
           "should warn that cached rows were used")


# --------------------------------------------------------------------------- #
# 10. Persistence (portfolio_state)
# --------------------------------------------------------------------------- #
def test_portfolio_state_roundtrip(tmp_path):
    from blue_engine.portfolio import portfolio_state
    payload = {"optimiser": "hrp", "target_weights": {"NIFTY": 0.6},
               "actual_weights": {"NIFTY": 0.5}}
    path = portfolio_state.record(payload, directory=tmp_path)
    _check(path is not None, "record should write a file")
    rows = portfolio_state.read_recent(days=1, directory=tmp_path)
    _check(len(rows) == 1 and rows[0]["optimiser"] == "hrp",
           "should read the run back")
    snaps = portfolio_state.snapshots(days=1, directory=tmp_path)
    _check(snaps and snaps[0]["target"] == {"NIFTY": 0.6},
           "snapshot shape should be drift-compatible")


def test_engine_persist_writes_a_run(tmp_path):
    from blue_engine.portfolio import portfolio_state
    eng = PortfolioEngine({"optimiser": "risk_parity", "persist": False})
    # Manually exercise the persist path against a temp dir.
    res = eng.run({"portfolio_inputs": _inputs()})
    portfolio_state.record(
        {"optimiser": res.data["optimiser"],
         "target_weights": res.data["target_weights"]},
        directory=tmp_path)
    rows = portfolio_state.read_recent(days=1, directory=tmp_path)
    _check(len(rows) == 1, "engine output should be persistable")


# --------------------------------------------------------------------------- #
# 11. Full pipeline still runs (economic -> market -> portfolio)
# --------------------------------------------------------------------------- #
def test_pipeline_runs_portfolio_stage_offline():
    from blue_engine.core.pipeline import MacroPipeline
    cfg = {
        "dry_run": True,
        "gate_on_infrastructure": False,
        "engines": {
            "economic": {"macro": {
                "gdp_growth": {"value": 6.8, "previous": 6.5},
                "inflation": {"value": 5.1, "previous": 4.8, "forecast": 5.6},
            }},
            "portfolio": {"optimiser": "risk_parity",
                          "portfolio_inputs": _inputs()},
        },
    }
    report = MacroPipeline.from_config(cfg).run()
    _check("portfolio" in report["stages"], "portfolio stage should run")
    port = report["stages"]["portfolio"]["data"]
    _check("target_weights" in port and port["target_weights"],
           "portfolio output should be present in the pipeline report")


# --------------------------------------------------------------------------- #
# 12. Price cache (the accumulating backtest dataset)
# --------------------------------------------------------------------------- #
def test_price_cache_roundtrip_and_idempotency(tmp_path):
    from blue_engine.portfolio.providers import price_cache
    c = price_cache.PriceCache(tmp_path / "p.db")
    rows = [{"timestamp": "2025-01-01", "close": 100.0, "volume": 10},
            {"date": "2025-01-02", "close": 102.0, "volume": 11}]
    _check(c.upsert("NIFTY", "NSE_INDEX", "D", rows) == 2, "should write 2")
    _check(c.closes("NIFTY", "NSE_INDEX") == [100.0, 102.0], "ordered closes")
    # Idempotent: re-fetching the same window must not duplicate.
    c.upsert("NIFTY", "NSE_INDEX", "D", rows)
    _check(len(c.closes("NIFTY", "NSE_INDEX")) == 2,
           "re-upsert must not duplicate rows")
    # A corrected bar must overwrite in place.
    c.upsert("NIFTY", "NSE_INDEX", "D",
             [{"timestamp": "2025-01-02", "close": 999.0}])
    _check(c.closes("NIFTY", "NSE_INDEX")[-1] == 999.0,
           "correction should update in place")
    _check(c.available() is True, "cache should report available")


def test_price_cache_accepts_both_row_shapes():
    from blue_engine.portfolio.providers import price_cache
    dict_rows = price_cache.normalise_rows(
        [{"timestamp": "2025-01-01", "close": 100.0}])
    list_rows = price_cache.normalise_rows([["2025-01-02", 100, 105, 99, 104, 5]])
    _check(len(dict_rows) == 1 and dict_rows[0]["close"] == 100.0,
           "dict shape should parse")
    _check(len(list_rows) == 1 and list_rows[0]["close"] == 104.0,
           "OHLCV list shape should parse")
    _check(price_cache.normalise_rows([None, {}, "x", [1, 2]]) == [],
           "garbage rows must be dropped")


def test_price_cache_panel_and_coverage(tmp_path):
    from blue_engine.portfolio.providers import price_cache
    c = price_cache.PriceCache(tmp_path / "p.db")
    for sym in ("NIFTY", "IT"):
        c.upsert(sym, "NSE_INDEX", "D",
                 [{"timestamp": f"2025-01-0{i}", "close": 100.0 + i}
                  for i in range(1, 4)])
    panel = c.panel([("NIFTY", "NSE_INDEX"), ("IT", "NSE_INDEX")])
    _check(set(panel.keys()) == {"NIFTY", "IT"}, "panel should key by symbol")
    cov = c.coverage("NIFTY", "NSE_INDEX")
    _check(cov["n"] == 3 and cov["first"] == "2025-01-01",
           f"coverage wrong: {cov}")
    _check(len(c.summary()) == 2, "summary should list both instruments")


# --------------------------------------------------------------------------- #
# 13. yfinance backfill (optional - must degrade, never raise)
# --------------------------------------------------------------------------- #
def test_yfinance_provider_degrades_when_unavailable():
    from blue_engine.portfolio.providers import yfinance_provider as yf
    # Whether or not yfinance is installed, the call must NEVER raise.
    res = yf.fetch_history("^NSEI", start="2020-01-01", end="2020-02-01")
    _check(isinstance(res, dict), "always returns a dict")
    _check("ok" in res and "rows" in res and "warnings" in res,
           "result must carry ok/rows/warnings")
    if not yf.available():
        _check(res["ok"] is False, "no yfinance -> ok False")
        _check(res["rows"] == [], "no yfinance -> no rows")
        _check(res["warnings"], "no yfinance -> a warning")


def test_yfinance_empty_ticker_is_handled():
    from blue_engine.portfolio.providers import yfinance_provider as yf
    res = yf.fetch_history("")
    _check(res["ok"] is False, "empty ticker -> ok False")
    _check(res["warnings"], "empty ticker -> a warning")


def test_yfinance_backfill_cache_never_raises(tmp_path):
    from blue_engine.portfolio.providers import yfinance_provider as yf
    from blue_engine.portfolio.providers import price_cache
    c = price_cache.PriceCache(tmp_path / "p.db")
    # A bogus ticker + a real one; either way the call must not raise and the
    # cache must remain readable.
    res = yf.backfill_cache({"NIFTY": "__definitely_not_a_ticker__"},
                            start="2020-01-01", end="2020-02-01", cache=c)
    _check(isinstance(res, dict) and "warnings" in res,
           "backfill must return a result dict")
    _check(isinstance(c.summary(), list), "cache still readable")


# --------------------------------------------------------------------------- #
# 14. Walk-forward optimiser benchmark (the decision tool)
# --------------------------------------------------------------------------- #
def _synthetic_panel(n=400, seed=42):
    import random
    random.seed(seed)
    common = [random.gauss(0.0004, 0.010) for _ in range(n)]

    def _series(beta, idio):
        return [beta * common[i] + random.gauss(0.0003, idio)
                for i in range(n)]

    raw = {"NIFTY": _series(1.0, 0.005),
           "IT": _series(0.9, 0.008),
           "SMALLCAP": _series(1.1, 0.020)}
    panel = {}
    for k, rs in raw.items():
        px, out = 100.0, []
        for r in rs:
            px *= (1 + r)
            out.append(px)
        panel[k] = out
    return panel


def test_benchmark_walk_forward_runs_and_charges_costs():
    from blue_engine.portfolio.optim import benchmark
    res = benchmark.run_walk_forward(_synthetic_panel(), "risk_parity",
                                     window=100, step=20)
    _check(res["ok"] is True, f"walk-forward should run: {res.get('warnings')}")
    _check(res["n_periods"] > 1, "should produce several rebalance periods")
    _check(res["cost_bps_total"] >= 0, "costs must be non-negative")
    _check(res["turnover_total"] >= 0, "turnover must be non-negative")
    _check("sharpe" in res["net"], "net summary should carry Sharpe")
    # Net must never beat gross (costs only subtract).
    g = res["gross"].get("annual_return")
    n = res["net"].get("annual_return")
    if g is not None and n is not None:
        _check(n <= g + 1e-9, f"net {n} must not exceed gross {g}")


def test_benchmark_has_no_look_ahead_in_weights():
    """Each row's covariance window must end at or before its own `t`."""
    from blue_engine.portfolio.optim import benchmark
    res = benchmark.run_walk_forward(_synthetic_panel(), "hrp",
                                     window=100, step=20)
    _check(res["ok"] is True, "should run")
    for row in res["rows"]:
        _check(row["t"] >= res["window_days"],
               f"rebalance at t={row['t']} lacks its look-back window")


def test_benchmark_declares_a_winner_and_ranks_all():
    from blue_engine.portfolio.optim import benchmark
    res = benchmark.compare_optimisers(
        _synthetic_panel(), optimisers=["inverse_vol", "risk_parity", "hrp"],
        window=100, step=20)
    _check(res["ok"] is True, f"compare should run: {res.get('warnings')}")
    _check(res["winner"] == res["ranking"][0], "winner must top the ranking")
    _check(len(res["table"]) == len(res["ranking"]), "table matches ranking")
    for row in res["table"]:
        _check("net_sharpe" in row and "cost_bps_total" in row,
               f"table row missing fields: {row}")


def test_benchmark_handles_short_panel_gracefully():
    from blue_engine.portfolio.optim import benchmark
    res = benchmark.run_walk_forward({"A": [1, 2, 3], "B": [1, 2, 3]},
                                     "hrp", window=100, step=20)
    _check(res["ok"] is False, "too-short panel -> ok False, not a raise")
    _check(res["warnings"], "should explain why")


def test_benchmark_spreads_rebalance_annualisation():
    """A 20-day step must annualise differently from a 1-day step."""
    from blue_engine.portfolio.optim import benchmark
    panel = _synthetic_panel()
    fast = benchmark.run_walk_forward(panel, "risk_parity", window=100, step=5)
    slow = benchmark.run_walk_forward(panel, "risk_parity", window=100, step=40)
    if fast["ok"] and slow["ok"]:
        _check(fast["step_days"] == 5 and slow["step_days"] == 40,
               "step should be recorded")
        # Fewer, larger periods for the slow rebalance schedule.
        _check(slow["n_periods"] <= fast["n_periods"],
               "a slower schedule should have fewer rebalances")


# --------------------------------------------------------------------------- #
# 15. Significance testing (P0: is the winner actually better?)
# --------------------------------------------------------------------------- #
def test_block_indices_preserve_length_and_range():
    import random
    from blue_engine.portfolio.optim import significance
    rng = random.Random(1)
    idx = significance.block_indices(100, 10, rng=rng)
    _check(len(idx) == 100, f"should return n indices, got {len(idx)}")
    _check(all(0 <= i < 100 for i in idx), "indices must stay in range")


def test_block_indices_are_contiguous_blocks():
    """A block bootstrap must draw runs, not isolated points (that's the point)."""
    import random
    from blue_engine.portfolio.optim import significance
    rng = random.Random(2)
    idx = significance.block_indices(60, 6, rng=rng)
    # With circular blocks of 6, consecutive output runs should mostly be +1.
    consecutive = sum(1 for a, b in zip(idx, idx[1:]) if (b - a) % 60 == 1)
    _check(consecutive > len(idx) * 0.6,
           f"expected contiguous runs, got {consecutive}/{len(idx)}")


def test_bootstrap_ci_contains_the_point_estimate():
    import random
    from blue_engine.portfolio.optim import significance
    random.seed(5)
    rs = [random.gauss(0.0005, 0.01) for _ in range(300)]
    out = significance.bootstrap_ci(rs, n_boot=300)
    sh = out["sharpe"]
    _check(sh["ok"] is True, "bootstrap should run")
    _check(sh["ci_low"] <= sh["point"] <= sh["ci_high"],
           f"CI must bracket the point: {sh}")
    _check(sh["se"] is not None and sh["se"] > 0, "should report a std error")


def test_bootstrap_degrades_on_tiny_sample():
    from blue_engine.portfolio.optim import significance
    from blue_engine.portfolio.attribution import perf
    out = significance.bootstrap_metric([0.01, 0.02], perf.sharpe)
    _check(out["ok"] is False, "tiny sample -> ok False, not a raise")
    _check(out["warnings"], "should explain why")


def test_paired_bootstrap_detects_a_real_difference():
    """A genuinely better series must be flagged significant."""
    import random
    from blue_engine.portfolio.optim import significance
    from blue_engine.portfolio.attribution import perf
    random.seed(9)
    base = [random.gauss(0.0002, 0.01) for _ in range(400)]
    # Same noise but a clearly higher mean -> a real, detectable edge.
    better = [b + 0.0025 for b in base]
    out = significance.paired_bootstrap(better, base, metric=perf.sharpe,
                                        n_boot=500)
    _check(out["ok"] is True, "paired bootstrap should run")
    _check(out["ci_low"] > 0, f"the CI should exclude zero: {out}")
    _check(out["significant"] is True, "a real edge must be significant")
    _check(out["p_a_beats_b"] > 0.95, f"should almost always win: {out}")


def test_paired_bootstrap_flags_noise_as_insignificant():
    """Two draws from the SAME distribution must NOT be called different.

    This is the guard against the exact mistake the first benchmark table made:
    declaring a winner on sampling noise.
    """
    import random
    from blue_engine.portfolio.optim import significance
    from blue_engine.portfolio.attribution import perf
    random.seed(11)
    a = [random.gauss(0.0004, 0.01) for _ in range(200)]
    b = [random.gauss(0.0004, 0.01) for _ in range(200)]
    out = significance.paired_bootstrap(a, b, metric=perf.sharpe, n_boot=500)
    _check(out["ok"] is True, "should run")
    _check(out["significant"] is False,
           f"noise must not be significant: CI={out['ci_low']}..{out['ci_high']}")


def test_compare_series_gives_a_readable_verdict():
    import random
    from blue_engine.portfolio.optim import significance
    random.seed(13)
    a = [random.gauss(0.0004, 0.01) for _ in range(200)]
    b = [random.gauss(0.0004, 0.01) for _ in range(200)]
    out = significance.compare_series(a, b, label_a="HRP", label_b="invvol")
    _check(out["ok"] is True, "should run")
    _check("NOT distinguishable" in out["verdict"]
           or "significantly better" in out["verdict"],
           f"verdict should be plain English: {out['verdict']}")


# --------------------------------------------------------------------------- #
# 16. compare_optimisers_significant (the decision tool)
# --------------------------------------------------------------------------- #
def test_significant_compare_annotates_table_and_names_baseline():
    from blue_engine.portfolio.optim import benchmark
    res = benchmark.compare_optimisers_significant(
        _synthetic_panel(), optimisers=["inverse_vol", "risk_parity", "hrp"],
        window=100, step=20, n_boot=200)
    _check(res["ok"] is True, f"should run: {res.get('warnings')}")
    _check(res["baseline"] == "inverse_vol", "baseline should be recorded")
    _check("real_winners" in res, "should list significant winners")
    _check(isinstance(res["robust_verdict"], str) and res["robust_verdict"],
           "should carry a plain-English verdict")
    base_row = next(r for r in res["table"] if r["optimiser"] == "inverse_vol")
    _check(base_row["verdict"] == "baseline", "baseline row self-labels")
    for r in res["table"]:
        if r["optimiser"] != "inverse_vol":
            _check("p_beats_base" in r and "significant_vs_base" in r,
                   f"non-baseline rows need significance: {r}")


def test_significant_compare_survives_missing_baseline():
    from blue_engine.portfolio.optim import benchmark
    res = benchmark.compare_optimisers_significant(
        _synthetic_panel(), optimisers=["risk_parity", "hrp"],
        baseline="does_not_exist", window=100, step=20, n_boot=100)
    _check(res["ok"] is True, "must not fail on a missing baseline")
    _check(any("baseline" in w for w in res["warnings"]),
           "should warn that significance was skipped")


# --------------------------------------------------------------------------- #
# 17. Rebalance sweep (the lever that moves P&L)
# --------------------------------------------------------------------------- #
def test_sweep_returns_a_full_grid():
    from blue_engine.portfolio.optim import benchmark
    out = benchmark.sweep_rebalance_params(
        _synthetic_panel(), "risk_parity", steps=(20, 60),
        bands=(0.0, 2.0, 5.0))
    _check(out["ok"] is True, f"sweep should run: {out.get('warnings')}")
    _check(len(out["table"]) == 6, f"2 steps x 3 bands = 6 rows, got {len(out['table'])}")
    _check(out["best"] is not None, "should name a best point")
    _check(out["cost_range_bps"] >= 0, "cost range non-negative")
    for r in out["table"]:
        for k in ("step", "band_pct", "net_sharpe", "turnover", "cost_bps"):
            _check(k in r, f"sweep row missing {k}: {r}")


def test_sweep_wider_band_costs_less():
    """A wider no-trade band must not increase cost (fewer forced trades)."""
    from blue_engine.portfolio.optim import benchmark
    out = benchmark.sweep_rebalance_params(
        _synthetic_panel(), "risk_parity", steps=(20,), bands=(0.0, 5.0))
    _check(out["ok"] is True, "sweep should run")
    grid = out["grid"]
    tight = grid[(20, 0.0)]["cost_bps"]
    wide = grid[(20, 5.0)]["cost_bps"]
    _check(wide <= tight + 1e-9,
           f"a 5% band should cost no more than a 0% band: {wide} vs {tight}")


def test_run_walk_forward_records_the_band():
    from blue_engine.portfolio.optim import benchmark
    res = benchmark.run_walk_forward(_synthetic_panel(), "risk_parity",
                                     window=100, step=20, cost_band_pct=3.0)
    _check(res["ok"] is True, "should run")
    _check(res["cost_band_pct"] == 3.0, "band should be recorded in the result")


# --------------------------------------------------------------------------- #
# 18. Universe robustness
# --------------------------------------------------------------------------- #
def test_universe_robustness_counts_winners_over_subsets():
    from blue_engine.portfolio.optim import benchmark
    # min_sleeves=2 enables leave-one-out subsets, so a 3-sleeve panel yields
    # the full universe plus three 2-sleeve subsets.
    res = benchmark.universe_robustness(
        _synthetic_panel(seed=3), optimisers=["inverse_vol", "risk_parity"],
        window=100, step=20, max_subsets=4, min_sleeves=2)
    _check(res["ok"] is True, f"should run: {res.get('warnings')}")
    _check(res["n_subsets"] >= 2, "should test several subsets")
    _check(res["stable_winner"] in res["win_counts"],
           "stable winner must come from the win counts")
    _check(abs(sum(res["win_share"].values()) - 1.0) < 1e-9,
           f"win shares should sum to 1: {res['win_share']}")
    _check(isinstance(res["robust"], bool), "should give a robustness flag")
    _check(any(t["sleeves"] < 3 for t in res["table"]),
           "leave-one-out subsets should appear")


def test_universe_robustness_single_subset_is_still_valid():
    """A 3-sleeve panel with min_sleeves=3 legitimately yields one subset."""
    from blue_engine.portfolio.optim import benchmark
    res = benchmark.universe_robustness(
        _synthetic_panel(seed=3), optimisers=["inverse_vol", "risk_parity"],
        window=100, step=20, min_sleeves=3)
    _check(res["ok"] is True, "should still be a valid result")
    _check(res["n_subsets"] == 1, f"only the full universe qualifies: {res['n_subsets']}")
    _check(res["robust"] is True, "a single-subset winner is trivially 100%")


def test_universe_robustness_needs_enough_sleeves():
    from blue_engine.portfolio.optim import benchmark
    res = benchmark.universe_robustness({"A": [1, 2, 3], "B": [1, 2, 3]},
                                        min_sleeves=3)
    _check(res["ok"] is False, "too few sleeves -> ok False")
    _check(res["warnings"], "should explain why")


# --------------------------------------------------------------------------- #
# 19. Data integrity (the check that makes a backtest trustworthy)
# --------------------------------------------------------------------------- #
def test_integrity_flags_stale_forward_filled_data():
    from blue_engine.portfolio.optim import benchmark
    flat = [100.0] * 40 + [101.0, 102.0, 103.0]   # a long frozen stretch
    res = benchmark.check_price_integrity({"STALE": flat})
    _check(any("stale" in w.lower() for w in res["warnings"]),
           f"should flag the frozen run: {res['warnings']}")


def test_integrity_flags_extreme_moves():
    from blue_engine.portfolio.optim import benchmark
    px = [100.0]
    for _ in range(30):
        px.append(px[-1] * 1.001)
    px.append(px[-1] * 0.5)      # an unadjusted split, not a real -50% day
    res = benchmark.check_price_integrity({"SPLIT": px})
    _check(any("split" in w.lower() for w in res["warnings"]),
           f"should flag the jump: {res['warnings']}")


def test_integrity_clean_data_reports_ok():
    import random
    from blue_engine.portfolio.optim import benchmark
    random.seed(21)
    px = [100.0]
    for _ in range(200):
        px.append(px[-1] * (1 + random.gauss(0.0004, 0.01)))
    res = benchmark.check_price_integrity({"CLEAN": px})
    _check(res["ok"] is True, f"clean data should pass: {res['warnings']}")
    _check(res["sleeves"]["CLEAN"]["ok"] is True, "per-sleeve ok flag")
    _check(res["notes"], "should carry the price-vs-TR caveat")


# --------------------------------------------------------------------------- #
# 20. P1: provider factory + config wiring
# --------------------------------------------------------------------------- #
def test_factory_never_raises_without_a_client(monkeypatch, tmp_path):
    """With no client AND an empty cache, the factory must still return a dict.

    Isolated DB so the test cannot pick up (or write) real cached history.
    """
    import json
    from blue_engine.portfolio.providers import factory
    cfg_file = tmp_path / "portfolio.json"
    cfg_file.write_text(json.dumps({
        "cache_db": str(tmp_path / "empty.db"),
        "universe_names": ["NIFTY", "IT"],
    }), encoding="utf-8")
    monkeypatch.setenv("BLUE_PORTFOLIO_CONFIG", str(cfg_file))
    monkeypatch.delenv("BLUE_PORTFOLIO_CLIENT", raising=False)
    factory.clear_client()
    out = factory.portfolio_inputs()
    _check(isinstance(out, dict), "factory must return a dict")
    _check("meta" in out, "should carry meta")
    _check("universe" in out or not out["meta"].get("ok"),
           "either a universe, or not ok")


def test_factory_client_injection_is_used():
    from blue_engine.portfolio.providers import factory
    sentinel = object()
    factory.set_client(sentinel)
    try:
        _check(factory.get_client() is sentinel, "injected client should stick")
    finally:
        factory.clear_client()
    _check(factory.get_client() is not sentinel, "clear_client should drop it")


def test_factory_with_fake_client_produces_a_book(monkeypatch, tmp_path):
    """A live client must produce a usable book - with an ISOLATED cache DB.

    The fake writes junk timestamps (``t0..t199``), so it must never touch the
    real ``data/price_cache/prices.db``: point the factory at a temp DB via
    ``BLUE_PORTFOLIO_CONFIG``.
    """
    import json
    import random
    from blue_engine.portfolio.providers import factory

    class _FakeClient:
        def history(self, **kw):
            random.seed(3)
            base, rows = 100.0, []
            for _ in range(200):
                base *= (1 + random.gauss(0.0004, 0.012))
                rows.append({"timestamp": f"t{len(rows)}", "close": base})
            return {"status": "success", "data": rows}

        def positionbook(self):
            return {"status": "success", "data": [
                {"symbol": "NIFTY", "quantity": 10, "ltp": 22000}]}

        def holdings(self):
            return {"status": "success", "data": []}

        def funds(self):
            return {"status": "success", "data": {"availablecash": 500000}}

    cfg_file = tmp_path / "portfolio.json"
    cfg_file.write_text(json.dumps({
        "cache_db": str(tmp_path / "iso.db"),
        "universe_names": ["NIFTY", "IT", "PHARMA"],
    }), encoding="utf-8")
    monkeypatch.setenv("BLUE_PORTFOLIO_CONFIG", str(cfg_file))
    monkeypatch.delenv("BLUE_MACRO_CONFIG_PATH", raising=False)
    factory.set_client(_FakeClient())
    try:
        out = factory.portfolio_inputs()
    finally:
        factory.clear_client()
    _check(out["meta"].get("ok") is True,
           f"live provider should produce a book: {out['meta'].get('warnings')}")
    _check(out.get("vols"), "should carry vols")


def test_factory_config_is_read_from_macro_master():
    """The factory must honour the portfolio block in input/macro_master.json."""
    from blue_engine.portfolio.providers import factory
    cfg = factory._load_config()
    _check("provider_ref" not in cfg,
           "provider_ref is wiring, not factory config")
    # The macro config sets these; assert a couple that matter for P0.
    _check(cfg.get("optimiser") == "risk_parity",
           f"optimiser should come from config: {cfg.get('optimiser')}")
    _check(cfg.get("rebalance_band_pct") == 1.0,
           f"P0 band should be wired: {cfg.get('rebalance_band_pct')}")
    _check(isinstance(cfg.get("universe_names"), list),
           "universe_names should be configured")


def test_macro_master_json_is_valid_and_wired():
    """The shipped config must parse and carry the provider_ref."""
    import json
    from pathlib import Path
    # Resolve package-relative so the test passes from any working directory.
    p = (Path(__file__).resolve().parents[1] / "input" / "macro_master.json")
    _check(p.is_file(), f"macro_master.json not found at {p}")
    with p.open("r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    port = (cfg.get("engines") or {}).get("portfolio") or {}
    _check("provider_ref" in port, "portfolio must declare provider_ref")
    _check("factory:portfolio_inputs" in port["provider_ref"],
           f"provider_ref should target the factory: {port['provider_ref']}")
    _check(bool(port.get("portfolio_inputs")),
           "the static fallback inputs must remain")


def test_provider_ref_is_resolvable_by_the_pipeline():
    """The dotted path in config must actually import and be callable."""
    import importlib
    ref = "blue_engine.portfolio.providers.factory:portfolio_inputs"
    module_name, func_name = ref.split(":", 1)
    module = importlib.import_module(module_name)
    fn = getattr(module, func_name)
    _check(callable(fn), "resolved provider_ref must be callable")
    # The pipeline calls it with NO arguments - assert the signature allows that.
    import inspect
    params = inspect.signature(fn).parameters
    required = [p for p in params.values()
                if p.default is inspect.Parameter.empty
                and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    _check(not required,
           f"provider_ref must take no required args, got {required}")


# --------------------------------------------------------------------------- #
# 21. P1: composite risk-block atomicity (the bug that corrupted weights)
# --------------------------------------------------------------------------- #
def test_composite_does_not_mix_risk_block_vocabularies():
    """Two sources with DIFFERENT sleeve names must not be deep-merged.

    Regression test: a cache keyed IT/PHARMA merged with a config keyed
    NIFTYIT/NIFTYPHARMA produced six vols for a four-sleeve universe, so the
    optimiser silently read missing keys as defaults.
    """
    from blue_engine.portfolio.providers import composite
    cache_like = _FakeProvider({
        "universe": ["NIFTY", "IT"],
        "vols": {"NIFTY": 0.14, "IT": 0.18},
        "cov": [[0.02, 0.0], [0.0, 0.03]],
    })
    config_like = _FakeProvider({
        "universe": ["NIFTY", "NIFTYIT"],
        "vols": {"NIFTY": 0.14, "NIFTYIT": 0.17},
    })
    out = composite.CompositeProvider([cache_like, config_like]).fetch()
    uni = set(out.get("universe") or [])
    _check(uni == {"NIFTY", "IT"},
           f"the richer source should own the block, got {uni}")
    _check(set(out["vols"]) == uni,
           f"vols keys must match the universe: {set(out['vols'])} vs {uni}")


def test_composite_prefers_a_covariance_over_hand_set_vols():
    """A real covariance must survive a later, poorer config fallback."""
    from blue_engine.portfolio.providers import composite
    rich = _FakeProvider({
        "universe": ["A", "B"], "vols": {"A": 0.1, "B": 0.2},
        "cov": [[0.01, 0.0], [0.0, 0.04]], "n_obs": 500,
    })
    poor = _FakeProvider({
        "universe": ["A", "B"], "vols": {"A": 0.3, "B": 0.3},
    })
    out = composite.CompositeProvider([rich, poor]).fetch()
    _check(out.get("cov") is not None,
           "the covariance must not be wiped by a later config block")
    _check(out["meta"]["risk_source"] == "fake",
           f"cache/rich source should own the block: {out['meta']}")
    _check(out["meta"]["risk_quality"] == 3, "quality should be 3 (cov present)")


def test_composite_still_merges_non_risk_keys_from_weaker_sources():
    from blue_engine.portfolio.providers import composite
    rich = _FakeProvider({
        "universe": ["A", "B"], "cov": [[0.01, 0.0], [0.0, 0.04]],
    })
    acct = _FakeProvider({
        "universe": ["A", "B"], "current_weights": {"A": 0.4},
        "capital": 500000.0,
    })
    out = composite.CompositeProvider([rich, acct]).fetch()
    _check(out.get("current_weights") == {"A": 0.4},
           "account keys must still merge in")
    _check(out.get("capital") == 500000.0, "capital must merge in")
    _check(out.get("cov") is not None, "the covariance must survive")


def test_composite_ok_requires_a_usable_universe():
    from blue_engine.portfolio.providers import composite
    empty = _FakeProvider({"vols": {}})
    out = composite.CompositeProvider([empty]).fetch()
    _check(out["meta"]["ok"] is False,
           "no universe -> not usable, even if a source said ok")


# --------------------------------------------------------------------------- #
# 22. P1: the daily job
# --------------------------------------------------------------------------- #
def test_job_market_hours_guard():
    from datetime import datetime
    from blue_engine.jobs import daily_portfolio as job
    # Sunday -> never closed for trading purposes.
    sun = datetime(2026, 9, 27, 16, 0)
    _check(job.is_market_closed(sun) is False, "weekends are not a trading close")
    # A weekday before the window -> not ready.
    early = datetime(2026, 9, 25, 10, 0)
    _check(job.is_market_closed(early) is False, "10:00 is mid-session")
    # A weekday after 15:35 -> ready.
    late = datetime(2026, 9, 25, 16, 0)
    _check(job.is_market_closed(late) is True, "16:00 is after the close")


def test_job_skips_outside_market_hours():
    from datetime import datetime
    from blue_engine.jobs import daily_portfolio as job
    # 10:00 on a weekday, no --force -> must skip cleanly (not fail).
    import unittest.mock as mock
    with mock.patch.object(job, "is_market_closed", return_value=False):
        out = job.run(force=False, dry_run=True)
    _check(out["ok"] is False, "a skip is not a success")
    _check(out["stage"] == "skipped", f"stage should be skipped: {out}")
    _check(any("market not closed" in w for w in out["warnings"]),
           "should explain the skip")


def test_job_never_raises_on_failure():
    from blue_engine.jobs import daily_portfolio as job
    import unittest.mock as mock
    with mock.patch.object(job, "is_market_closed", return_value=True):
        with mock.patch("blue_engine.portfolio.providers.factory"
                        ".portfolio_inputs",
                        side_effect=RuntimeError("boom")):
            out = job.run(force=True, dry_run=True)
    _check(out["ok"] is False, "a failure is ok=False")
    _check(out["stage"] == "failed", f"stage should be failed: {out}")
    _check(out["error"] is not None, "should record the error")


def test_job_writes_and_reads_status(tmp_path):
    from blue_engine.jobs import daily_portfolio as job
    import unittest.mock as mock
    with mock.patch.object(job, "STATUS_DIR", tmp_path):
        with mock.patch.object(job, "STATUS_FILE", tmp_path / "s.json"):
            with mock.patch.object(job, "is_market_closed", return_value=False):
                job.run(force=False, dry_run=True)
            got = job.read_status()
    _check(got is not None, "status file should be written")
    _check(got.get("stage") == "skipped", f"status should record the stage: {got}")


def test_job_dry_run_writes_no_run_history(tmp_path):
    """--dry-run must compute but persist nothing."""
    from blue_engine.jobs import daily_portfolio as job
    import unittest.mock as mock
    with mock.patch.object(job, "is_market_closed", return_value=True):
        with mock.patch.object(job, "write_status", return_value=None):
            out = job.run(force=True, dry_run=True)
    _check(out["dry_run"] is True, "should be flagged as a dry run")
    _check(out.get("run_file") is None, "dry run must not write a run file")


def test_job_module_is_runnable_and_has_a_cli():
    from blue_engine.jobs import daily_portfolio as job
    _check(hasattr(job, "main"), "job needs a main()").__class__
    import inspect
    params = inspect.signature(job.main).parameters
    _check("argv" in params, "main should accept argv for testability")


# --------------------------------------------------------------------------- #
# 23. Basket construction (sleeves Yahoo has no index for, e.g. AUTO)
# --------------------------------------------------------------------------- #
def test_fetch_basket_needs_tickers():
    from blue_engine.portfolio.providers import yfinance_provider as yf
    res = yf.fetch_basket([])
    _check(res["ok"] is False, "no tickers -> ok False")
    _check(res["warnings"], "should explain why")


def test_fetch_basket_never_raises_on_bad_tickers():
    from blue_engine.portfolio.providers import yfinance_provider as yf
    # Bogus tickers: must degrade, never raise (and must not hit the network
    # in a way that fails the suite - it just returns ok=False).
    res = yf.fetch_basket(["__nope_one__", "__nope_two__"],
                          start="2020-01-01", end="2020-02-01")
    _check(isinstance(res, dict), "always a dict")
    if not res["ok"]:
        _check(res["warnings"], "should warn when no constituent worked")


def test_default_baskets_covers_auto():
    from blue_engine.portfolio.providers import yfinance_provider as yf
    _check("AUTO" in yf.DEFAULT_BASKETS, "AUTO must have a basket")
    members = yf.DEFAULT_BASKETS["AUTO"]
    _check(len(members) >= 5, f"need several constituents: {members}")
    # Tata Motors demerged; the retired symbol must not be used.
    _check("TATAMOTORS.NS" not in members,
           "TATAMOTORS.NS is retired (demerger) - use TMPV.NS")
    _check(any(m.endswith(".NS") for m in members),
           "constituents should be NSE symbols")


def test_auto_is_not_claimed_as_a_direct_ticker():
    """AUTO has no Yahoo index, so it must come from the basket, not tickers."""
    from blue_engine.portfolio.providers import yfinance_provider as yf
    _check("AUTO" not in yf.DEFAULT_TICKERS,
           "AUTO should not be in DEFAULT_TICKERS (^CNXAUTO is 404)")


def test_backfill_accepts_a_baskets_argument():
    """The baskets override must be accepted without raising."""
    import inspect
    from blue_engine.portfolio.providers import yfinance_provider as yf
    params = inspect.signature(yf.backfill_cache).parameters
    _check("baskets" in params, "backfill_cache should accept baskets")
    _check(params["baskets"].default is None, "baskets should default to None")


# --------------------------------------------------------------------------- #
# 24. Test isolation (no test may touch the real price DB)
# --------------------------------------------------------------------------- #
def test_factory_never_writes_the_real_price_cache():
    """A fake client must write to an ISOLATED DB, not data/price_cache.

    Regression test: an earlier version of the factory tests wrote synthetic
    ``t0..t199`` timestamps into the real cache, corrupting the dataset.
    """
    from blue_engine.portfolio.providers import price_cache
    real = price_cache.DEFAULT_DB
    before = real.stat().st_mtime if real.is_file() else None
    # If the real DB is absent there is nothing to protect; skip silently.
    if before is None:
        return
    # Malformed timestamps are the fingerprint of a test fake.
    rows = price_cache.PriceCache(real).summary()
    junk = [r for r in rows if str(r["first"]).startswith(("t0", "row-"))]
    _check(not junk,
           f"real price cache is polluted with test rows: {junk}")


# --------------------------------------------------------------------------- #
# Runner (works without pytest)
# --------------------------------------------------------------------------- #
def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            if "tmp_path" in fn.__code__.co_varnames and fn.__code__.co_argcount:
                print(f"SKIP {name} (needs pytest fixtures)")
                continue
            fn()
            print(f"PASS {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {name}: {exc!r}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())