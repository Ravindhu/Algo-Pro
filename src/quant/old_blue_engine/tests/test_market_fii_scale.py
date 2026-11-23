"""FII flow scaling tests (2026-10-22).

Why these exist
---------------
``fii_scale`` was 1000 Cr. Measured over 170 real trading days of vendor FII
cash flow, ``|net| >= 1000`` on **82 %** of days, so ``fii_score`` pinned at
+/-1 almost every session: a -1,305 Cr day scored identically to -5,000 Cr.
The signal was a binary bought/sold flag wearing a magnitude signal's clothes.

The scale is now 6000 Cr (~95th percentile of |net|), which keeps the score
continuous across normal flow while still saturating on genuine one-way days.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]  # tests -> blue_engine -> quant -> src -> repo
if str(ROOT / "src" / "quant" / "blue_engine") not in sys.path:
    sys.path.insert(0, str(ROOT / "src" / "quant" / "blue_engine"))

from blue_engine.market.market_engine import MarketEngine  # noqa: E402


def _score(net_cash, scale=None, net_futures=None):
    cfg = {"indices": []}
    if scale is not None:
        cfg["fii_scale"] = scale
    fii = {"net_cash": net_cash}
    if net_futures is not None:
        fii["net_futures"] = net_futures
    return MarketEngine(cfg, provider=lambda: {"fii": fii}).run({}).data["fii_score"]


# ---------------------------------------------------------------------------
# The numeric contract
# ---------------------------------------------------------------------------
def test_scale_is_the_full_strength_point():
    """|flow| == scale -> +/-1, by definition."""
    assert _score(6000, 6000) == pytest.approx(1.0)
    assert _score(-6000, 6000) == pytest.approx(-1.0)


def test_normal_day_is_not_saturated():
    """-1,305 Cr must NOT score -1.0 (it did at scale=1000)."""
    score = _score(-1305.13, 6000)
    assert score == pytest.approx(-0.2175, abs=1e-3)
    assert abs(score) < 0.999, "a normal day must stay inside the clamp"


def test_old_scale_saturated_these_days():
    """Documents the regression: at 1000 every one of these scored -1.0."""
    for flow in (-1305.13, -1708.48, -2961.0, -3569.0):
        assert _score(flow, 1000) == pytest.approx(-1.0)


def test_magnitude_is_distinguishable_now():
    """Small and large outflows must produce different scores."""
    small = _score(-1000, 6000)
    large = _score(-5000, 6000)
    assert small > large, "a bigger outflow must score more negative"


def test_score_is_monotonic_in_flow():
    values = [-8000, -6000, -3000, -1000, 0, 1000, 3000, 6000, 8000]
    scores = [_score(v, 6000) for v in values]
    assert scores == sorted(scores), scores


def test_clamped_at_both_ends():
    assert _score(-50000, 6000) == pytest.approx(-1.0)
    assert _score(50000, 6000) == pytest.approx(1.0)


def test_zero_flow_is_neutral():
    assert _score(0, 6000) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Config handling
# ---------------------------------------------------------------------------
def test_default_scale_is_6000_when_config_omits_it():
    """The hardcoded fallback must agree with macro_master.json."""
    assert _score(-6000) == pytest.approx(-1.0)


def test_config_value_overrides_the_default():
    assert _score(-3000, 3000) == pytest.approx(-1.0)
    assert _score(-3000, 12000) == pytest.approx(-0.25)


@pytest.mark.parametrize("bad", [0, -5, "abc", None])
def test_invalid_scale_does_not_produce_a_clamped_score(bad):
    """A zero/negative/garbage scale must not divide-by-zero into a clamped -1.

    AUDIT (2026-10-22): there are two layers to check.

    1. ``_fii_score`` called directly with a bad scale returns ``None`` - the
       unit contract. A non-numeric string (``"abc"``) previously raised
       ``TypeError`` on the ``scale <= 0`` comparison and crashed the engine.
    2. ``_run`` sanitises a bad *configured* scale to the safe 6000 default
       before scoring, so the pipeline degrades to a finite, non-saturated
       score instead of silently dropping the FII signal.
    """
    engine = MarketEngine({"indices": []})
    # Layer 1: the scoring primitive must reject a bad scale cleanly.
    assert engine._fii_score({"net_cash": -1000}, bad) is None
    # Layer 2: the engine run falls back to 6000, so the score is finite and
    # nowhere near the clamp (a divide-by-zero would pin it at -1.0).
    score = _score(-1000, bad)
    assert score is not None
    assert -1.0 < score < 1.0


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
def test_cash_and_futures_are_averaged():
    # (-3000 + -9000) / 2 = -6000 -> full strength
    assert _score(-3000, 6000, net_futures=-9000) == pytest.approx(-1.0)


def test_missing_fii_block_returns_none():
    engine = MarketEngine({"indices": []})
    assert engine._fii_score({}, 6000.0) is None


def test_missing_flow_warns_and_falls_back_to_breadth():
    engine = MarketEngine({"indices": []}, provider=lambda: {
        "indices": {"NIFTY": {"close": 100, "prev_close": 99}}})
    result = engine.run({})
    assert result.data["fii_score"] is None
    assert any("no FII flow" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Live config must match the code default
# ---------------------------------------------------------------------------
def test_shipped_config_uses_6000():
    import json
    cfg = json.loads(
        (ROOT / "src" / "quant" / "blue_engine" / "input" / "macro_master.json")
        .read_text(encoding="utf-8"))
    assert cfg["engines"]["market"]["fii_scale"] == 6000.0
