"""
tests - economic engine test suite.

Run with::

    & ..\\openalgo\\.venv\\Scripts\\python.exe -m pytest tests -q

or without pytest (it also runs as a plain script)::

    & ..\\openalgo\\.venv\\Scripts\\python.exe tests\\test_economic_engine.py

Design
------
* Pure engines are tested with NO network / SDK - that is the whole point of the
  provider-injection design.
* Degradation paths (missing data, no numpy/sklearn) are asserted, because the
  engine's contract is "degrade, never halt".
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

from blue_engine.economics import (  # noqa: E402
    EconomicEngine, features, models, nlp, providers, regime, scoring)
from blue_engine.economics.providers import (  # noqa: E402
    CachedProvider, CompositeProvider, MacroProvider, OpenAlgoMacroProvider,
    value_block)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
FULL_MACRO = {
    "gdp_growth": {"value": 6.8, "previous": 6.5},
    "bond_return": {"value": 7.1, "previous": 7.0},
    "debt": {"value": 82.0, "previous": 81.5},
    "employment": {"value": 7.6, "previous": 7.9},
    "inflation": {"value": 5.1, "previous": 4.8, "forecast": 5.6},
    "climate": {"value": 42.0, "previous": 40.0},
    "tax": {"value": 27.0, "previous": 26.5},
    "currency": {"value": 84.5, "previous": 83.9},
    "forex_reserves": {"value": 690.0, "previous": 685.0},
}


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------- #
# 1. Engine contract
# --------------------------------------------------------------------------- #
def test_engine_never_raises_and_returns_engineresult():
    eng = EconomicEngine({"macro": FULL_MACRO})
    res = eng.run()
    _check(res.ok is True, "engine should be ok")
    _check(res.engine == "economic", "engine name should be 'economic'")
    for key in ("regime", "risk_on_score", "gross_exposure_multiplier",
                "signals", "signals_available"):
        _check(key in res.data, f"missing output key '{key}'")


def test_empty_macro_is_neutral_not_error():
    res = EconomicEngine({}).run()
    _check(res.ok is True, "empty macro must not fail the engine")
    _check(res.data["regime"] == "UNKNOWN", "empty macro -> UNKNOWN regime")
    _check(res.data["gross_exposure_multiplier"] == 1.0, "empty -> 1.0x gross")
    _check(any("no macro data" in w for w in res.warnings), "should warn")


def test_missing_signal_warns_but_scores_rest():
    macro = {k: v for k, v in FULL_MACRO.items() if k != "inflation"}
    res = EconomicEngine({"macro": macro}).run()
    _check(res.ok is True, "missing one signal must not fail")
    _check(any("inflation" in w for w in res.warnings), "should warn on missing")


def test_multiplier_is_clamped():
    # Extreme risk-on macro should not exceed the cap.
    hot = {k: {"value": 999.0, "previous": 1.0} for k in FULL_MACRO}
    hot["gdp_growth"] = {"value": 999.0, "previous": 1.0}
    res = EconomicEngine({"macro": hot}).run()
    m = res.data["gross_exposure_multiplier"]
    _check(regime.MULTIPLIER_FLOOR <= m <= regime.MULTIPLIER_CAP,
           f"multiplier {m} outside [floor, cap]")


# --------------------------------------------------------------------------- #
# 2. Surprise (weakness #1 fix) - the headline case study
# --------------------------------------------------------------------------- #
def test_cpi_below_forecast_is_risk_on():
    """CPI 5.1 vs 5.6 forecast -> surprise is NEGATIVE -> risk-ON for inflation
    (direction -1). This is the plan's worked example."""
    s = features.surprise.surprise({"value": 5.1, "forecast": 5.6})
    _check(s["method"] == "consensus", "should use consensus forecast")
    _check(abs(s["surprise"] + 0.5) < 1e-9, f"surprise should be -0.5, got {s}")
    # tanh(-z * direction=-1) is positive -> risk-on.
    scored = scoring.score_signal(direction=-1, level=5.1, scale=6.0,
                                  z_surprise=s["z"])
    _check(scored["views"]["surprise"] > 0,
           "a below-forecast CPI must score risk-ON")


def test_cpi_above_forecast_is_risk_off():
    s = features.surprise.surprise({"value": 6.1, "forecast": 5.6})
    _check(s["surprise"] > 0, "above-forecast CPI -> positive surprise")
    scored = scoring.score_signal(direction=-1, level=6.1, scale=6.0,
                                  z_surprise=s["z"])
    _check(scored["views"]["surprise"] < 0,
           "an above-forecast CPI must score risk-OFF")


def test_ar1_forecast_fallback_without_consensus():
    series = [1.0, 1.1, 1.2, 1.3, 1.4]
    block = {"value": 1.5, "previous": 1.4,
             "history": [{"value": v} for v in series]}
    s = features.surprise.surprise(block)
    _check(s["method"] == "ar1", f"should fall back to AR(1), got {s['method']}")
    _check(s["forecast"] is not None, "AR(1) should produce a forecast")


def test_change_fallback_uses_previous():
    """With only value+previous (the everyday live case) the surprise view must
    still populate, via the previous value as the expectation."""
    s = features.surprise.surprise({"value": 5.1, "previous": 4.8})
    _check(s["method"] == "change", f"expected 'change', got {s['method']}")
    _check(s["forecast"] == 4.8, "forecast should be the previous value")
    _check(abs(s["surprise"] - 0.3) < 1e-9, f"surprise should be 0.3, got {s}")
    _check(s["z"] is not None, "z must be populated so the view fills")


def test_surprise_priority_order():
    """consensus > ar1 > change."""
    consensus = features.surprise.surprise(
        {"value": 5.1, "previous": 4.8, "forecast": 5.6})
    _check(consensus["method"] == "consensus", "consensus wins")
    ar1 = features.surprise.surprise(
        {"value": 1.5, "previous": 1.4,
         "history": [{"value": v} for v in [1.0, 1.1, 1.2, 1.3, 1.4]]})
    _check(ar1["method"] == "ar1", "ar1 beats change when history present")


def test_bare_value_has_no_surprise():
    """A bare number with no prior cannot form an expectation -> 'none'."""
    s = features.surprise.surprise(5.1)
    _check(s["method"] == "none" and s["z"] is None,
           "bare value with no prior -> no surprise")


def test_engine_populates_surprise_view_for_all_signals():
    """Every signal that carries value+previous must have a populated S view."""
    from blue_engine.economics import EconomicEngine
    res = EconomicEngine({"macro": FULL_MACRO}).run()
    missing = []
    for name, s in res.data["signals"].items():
        if "surprise" not in (s.get("views") or {}):
            missing.append(name)
    _check(not missing,
           f"surprise view missing for signals: {missing} (should be all)")


def test_surprise_missing_value_is_safe():
    s = features.surprise.surprise({"forecast": 5.0})
    _check(s["method"] == "none" and s["z"] is None, "missing value -> safe none")


# --------------------------------------------------------------------------- #
# 3. Delevel
# --------------------------------------------------------------------------- #
def test_delevel_change_direction():
    up = features.delevel.z_of_change({"value": 10.0, "previous": 9.0})
    down = features.delevel.z_of_change({"value": 8.0, "previous": 9.0})
    _check(up["direction"] == "up", "rise -> 'up'")
    _check(down["direction"] == "down", "fall -> 'down'")
    _check(up["delta"] == 1.0 and down["delta"] == -1.0, "deltas wrong")


# --------------------------------------------------------------------------- #
# 4. Regime banding + multiplier
# --------------------------------------------------------------------------- #
def test_regime_band_cutpoints():
    _check(regime.band(0.40)[0] == "RISK_ON", "+0.40 -> RISK_ON")
    _check(regime.band(0.35)[0] == "RISK_ON", "exact cut -> RISK_ON")
    _check(regime.band(0.0)[0] == "NEUTRAL", "0.0 -> NEUTRAL")
    _check(regime.band(-0.20)[0] == "MILD_RISK_OFF", "-0.20 -> MILD_RISK_OFF")
    _check(regime.band(-0.90)[0] == "RISK_OFF", "-0.90 -> RISK_OFF")


def test_posterior_multiplier_is_confidence_aware():
    _check(abs(regime.posterior_multiplier(0.5) - 1.0) < 1e-9,
           "p=0.5 -> neutral 1.0x")
    _check(regime.posterior_multiplier(1.0) == regime.MULTIPLIER_CAP,
           "p=1.0 -> cap")
    _check(regime.posterior_multiplier(0.0) == regime.MULTIPLIER_FLOOR,
           "p=0.0 -> floor")


def test_combine_multiplier_default_is_baseline():
    info = regime.combine_multiplier(0.4, None, model_weight=1.0)
    _check(info["path"] == "score", "no posterior -> score path")
    _check(abs(info["multiplier"] - regime.score_multiplier(0.4)) < 1e-9,
           "should equal the score path")


# --------------------------------------------------------------------------- #
# 5. Models (numpy-only HMM / kmeans) - skip cleanly if numpy missing
# --------------------------------------------------------------------------- #
def test_hmm_detects_persistent_states():
    if not models.hmm_regime.available():
        print("  (numpy absent: skipping HMM test)")
        return
    # Two clear regimes: a low-score stretch then a high-score stretch.
    obs = [-0.6] * 40 + [0.6] * 40
    res = models.hmm_regime.detect_regime(obs, n_states=2)
    _check(res["method"] == "hmm", f"should fit, got {res['method']}")
    _check(res["label"] in ("RISK_ON", "RISK_OFF", "NEUTRAL"),
           "label should be set")
    _check(res["p_risk_on"] is not None, "posterior should be present")


def test_hmm_insufficient_data_is_safe():
    res = models.hmm_regime.detect_regime([0.1, 0.2], n_states=3)
    _check(res["method"] in ("insufficient_data", "unavailable"),
           f"short history must degrade, got {res['method']}")


def test_cluster_labels_states():
    if not models.cluster.available():
        print("  (numpy absent: skipping cluster test)")
        return
    rows = [[-0.5], [-0.4], [-0.6], [0.5], [0.6], [0.4], [0.55]]
    res = models.cluster.cluster_states(rows, n_clusters=2)
    _check(res["method"] == "kmeans", f"should cluster, got {res['method']}")
    _check(res["label"] in ("RISK_ON", "RISK_OFF"), "last row should label")


# --------------------------------------------------------------------------- #
# 6. Classifier walk-forward (no look-ahead)
# --------------------------------------------------------------------------- #
def test_forward_labels_drop_unknown_tail():
    prices = [100.0, 101, 102, 103, 104, 105]
    labels = models.classifier.forward_return_labels(prices, horizon=2)
    _check(labels[-1] is None and labels[-2] is None,
           "last `horizon` labels must be None (unknown future)")
    _check(labels[0] == 1, "rising series -> label 1")


def test_walk_forward_runs_or_degrades():
    # Enough rows for at least one fold; features arbitrary but deterministic.
    feats = [[i / 100.0, (i % 7) / 7.0] for i in range(120)]
    prices = [100 + i * 0.5 for i in range(120)]
    labels = models.classifier.forward_return_labels(prices, horizon=5)
    res = models.classifier.walk_forward(feats, labels, train_size=60,
                                         test_size=30, kind="logit")
    _check(res["method"] in ("walk_forward", "insufficient_data",
                             "unavailable", "no_folds"),
           f"unexpected method {res['method']}")


# --------------------------------------------------------------------------- #
# 7. NLP
# --------------------------------------------------------------------------- #
def test_sentiment_scores_polarity():
    pos = nlp.sentiment.score_text("Stocks rally as RBI turns dovish")
    neg = nlp.sentiment.score_text("Market plunge amid hawkish tightening")
    _check(pos["score"] > 0, f"positive text should score > 0, got {pos}")
    _check(neg["score"] < 0, f"negative text should score < 0, got {neg}")


def test_negation_flips_sign():
    a = nlp.sentiment.score_text("growth is strong")
    b = nlp.sentiment.score_text("growth is not strong")
    _check(b["score"] < a["score"], "negation should lower the score")


def test_event_calendar_detects_monthly_expiry():
    from datetime import date
    res = nlp.event_calendar.upcoming_events(date(2025, 1, 20),
                                              within_days=15)
    names = [e["name"] for e in res["events"]]
    _check("FNO_EXPIRY" in names, f"should find monthly expiry, got {names}")
    _check(0.0 <= res["event_risk"] <= 1.0, "event_risk must be in [0,1]")


def test_engine_uses_headlines_for_news_tone():
    eng = EconomicEngine({"macro": FULL_MACRO})
    res = eng.run(context={"headlines": ["Sensex rallies", "FII inflow surge"]})
    _check("news_tone" in res.data["signals"],
           "headlines should add a news_tone signal")
    _check(res.data["nlp"] is not None, "nlp summary should be present")


# --------------------------------------------------------------------------- #
# 8. Providers (no network - fake client / fake provider)
# --------------------------------------------------------------------------- #
class _FakeProvider(MacroProvider):
    name = "fake"

    def __init__(self, payload, name="fake"):
        super().__init__({})
        self.name = name
        self._payload = payload

    def _fetch(self):
        return dict(self._payload)


class _ExplodingProvider(MacroProvider):
    name = "boom"

    def _fetch(self):
        raise RuntimeError("network down")


def test_provider_fetch_never_raises():
    res = _ExplodingProvider().fetch()
    _check(res["meta"]["ok"] is False, "exploding provider -> ok False")
    _check(res["meta"]["warnings"], "should carry a warning")


def test_composite_priority_first_wins():
    high = _FakeProvider({"inflation": value_block(5.0)}, name="high")
    low = _FakeProvider({"inflation": value_block(9.9), "gdp_growth":
                         value_block(7.0)}, name="low")
    comp = CompositeProvider([high, low])
    out = comp.fetch()
    _check(out["inflation"]["value"] == 5.0, "higher priority value should win")
    _check(out["gdp_growth"]["value"] == 7.0, "fallback should fill gaps")
    _check(out["meta"]["provenance"]["inflation"] == "high", "provenance wrong")


def test_cache_serves_stale_on_failure(tmp_path):
    # First fetch succeeds and is written to disk; second (exploding) source
    # should be served the cached copy with meta.stale=True.
    ok = _FakeProvider({"currency": value_block(84.5)}, name="c")
    cached = CachedProvider(ok, ttl_seconds=0.0, cache_dir=tmp_path)
    first = cached.fetch()
    _check(first["currency"]["value"] == 84.5, "first fetch should succeed")

    cached.source = _ExplodingProvider()          # now the live fed dies
    cached._mem = None                            # force a cache miss
    second = cached.fetch()
    _check(second["currency"]["value"] == 84.5,
           "should serve cached value on failure")
    _check(second["meta"]["stale"] is True, "stale flag should be set")


def test_openalgo_provider_with_fake_client():
    class _FakeClient:
        def history(self, **kw):
            return {"status": "success", "data": [
                {"timestamp": 1, "close": 83.0},
                {"timestamp": 2, "close": 84.5}]}

    prov = OpenAlgoMacroProvider(_FakeClient())
    out = prov.fetch()
    _check(out["meta"]["ok"] is True, "should succeed with a fake client")
    _check(out["currency"]["value"] == 84.5, "should read the last close")
    _check(out["currency"]["previous"] == 83.0, "should read previous close")


def test_openalgo_provider_handles_sdk_error():
    class _BadClient:
        def history(self, **kw):
            return {"status": "error", "message": "no data"}

    prov = OpenAlgoMacroProvider(_BadClient())
    out = prov.fetch()
    _check(out["meta"]["ok"] is False, "SDK error -> ok False, not a raise")
    _check(any("no history" in w for w in out["meta"]["warnings"]),
           "should warn about missing history")


# --------------------------------------------------------------------------- #
# 9. Secrets (env-first; never hardcoded)
# --------------------------------------------------------------------------- #
def test_secret_resolution_env_first(monkeypatch=None):
    name = "BLUE_ENGINE_TEST_KEY"
    os.environ[name] = "abc123def456"
    try:
        val = providers.get_secret(name)
        _check(val == "abc123def456", "env var should win")
        red = providers.redact(name, val)
        _check("abc123def456" not in red, "redact must not leak the key")
        _check("***56" in red, f"redact should show only the tail, got {red}")
    finally:
        os.environ.pop(name, None)


# --------------------------------------------------------------------------- #
# 10. Full pipeline still works end-to-end (pure, offline)
# --------------------------------------------------------------------------- #
def test_pipeline_runs_economic_stage_offline():
    from blue_engine.core.pipeline import MacroPipeline
    cfg = {
        "dry_run": True,
        "gate_on_infrastructure": False,
        "engines": {
            "economic": {"macro": FULL_MACRO},
        },
    }
    report = MacroPipeline.from_config(cfg).run()
    _check("economic" in report["stages"], "economic stage should run")
    econ = report["stages"]["economic"]["data"]
    _check("regime" in econ and "gross_exposure_multiplier" in econ,
           "economic output should be present in the pipeline report")


# --------------------------------------------------------------------------- #
# Runner (works without pytest)
# --------------------------------------------------------------------------- #
def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            # tmp_path-dependent tests need pytest; skip cleanly here.
            if "tmp_path" in fn.__code__.co_varnames and \
                    fn.__code__.co_argcount:
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