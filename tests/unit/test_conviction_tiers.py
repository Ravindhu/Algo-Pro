"""Unit tests for conviction tiers (A2) and hard-cap wiring (A3).

Maths and rules only. Profit is tested later, out-of-sample (step A4).
"""
import itertools
from pathlib import Path

import pytest

from quant.portfolio.tiers import (
    DEFAULT_CONFIG,
    TierTable,
    decide_size,
    load_tiers,
    tier_multiplier,
)

BANDS = ((0.5, 1.0), (0.7, 1.5), (0.85, 2.0))
ENABLED = TierTable(enabled=True, bands=BANDS, max_multiplier=2.0)
DISABLED = TierTable(enabled=False, bands=BANDS, max_multiplier=2.0)
CAPS = {"kelly": 100.0, "vol_target": 150.0, "drawdown": 120.0}


# --- config ---------------------------------------------------------------

def test_default_config_loads_and_is_disabled():
    table = load_tiers()
    assert DEFAULT_CONFIG.exists()
    assert table.enabled is False  # must stay off until A4 evidence exists


def test_config_rejects_enabled_without_bands(tmp_path: Path):
    bad = tmp_path / "t.yaml"
    bad.write_text("enabled: true\nbands: []\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_tiers(bad)


def test_config_rejects_non_bool_enabled(tmp_path: Path):
    bad = tmp_path / "t.yaml"
    bad.write_text("enabled: 'yes'\nbands:\n  - {min_score: 0.5, multiplier: 1}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_tiers(bad)


def test_config_caps_band_multiplier_at_max(tmp_path: Path):
    cfg = tmp_path / "t.yaml"
    cfg.write_text("enabled: true\nmax_multiplier: 1.5\nbands:\n"
                   "  - {min_score: 0.5, multiplier: 3.0}\n", encoding="utf-8")
    assert load_tiers(cfg).bands == ((0.5, 1.5),)


# --- tier multiplier ------------------------------------------------------

def test_disabled_tiers_always_return_one():
    for score in (0.0, 0.3, 0.6, 0.9, 1.0):
        assert tier_multiplier(score, DISABLED) == 1.0


@pytest.mark.parametrize("score,expected", [
    (0.0, 0.0),    # below lowest band: no trade
    (0.49, 0.0),
    (0.50, 1.0),   # band boundary is inclusive
    (0.69, 1.0),
    (0.70, 1.5),
    (0.84, 1.5),
    (0.85, 2.0),
    (1.0, 2.0),
])
def test_band_boundaries(score, expected):
    assert tier_multiplier(score, ENABLED) == expected


def test_tier_multiplier_rejects_out_of_range_score():
    with pytest.raises(ValueError):
        tier_multiplier(1.2, ENABLED)
    with pytest.raises(ValueError):
        tier_multiplier(float("nan"), ENABLED)


# --- decide_size: caps always win -----------------------------------------

def test_disabled_tiers_give_flat_size_limited_by_lowest_cap():
    d = decide_size(base_size=80.0, conviction_score=0.99, caps=CAPS, table=DISABLED)
    assert d.tier_multiplier == 1.0
    assert d.size == pytest.approx(80.0)          # under all caps, so flat
    assert d.tiers_enabled is False


def test_enabled_tier_can_raise_size_but_never_above_cap():
    d = decide_size(base_size=80.0, conviction_score=0.9, caps=CAPS, table=ENABLED)
    assert d.tier_multiplier == 2.0
    assert d.size == pytest.approx(100.0)         # 160 wanted, kelly cap is 100
    assert d.binding == "kelly"


def test_score_below_lowest_band_means_zero_size():
    d = decide_size(base_size=80.0, conviction_score=0.2, caps=CAPS, table=ENABLED)
    assert d.size == 0.0
    assert d.binding == "conviction_tier"


def test_regime_factor_de_risks_and_is_reported():
    d = decide_size(base_size=80.0, conviction_score=0.6, caps=CAPS, table=ENABLED,
                    regime_factor=0.5)
    assert d.size == pytest.approx(40.0)
    assert d.binding == "regime"


def test_regime_factor_cannot_increase_size():
    with pytest.raises(ValueError):
        decide_size(80.0, 0.6, CAPS, ENABLED, regime_factor=1.5)


def test_binding_reports_none_when_nothing_limits():
    d = decide_size(base_size=10.0, conviction_score=0.6, caps=CAPS, table=DISABLED)
    assert d.binding == "none"


def test_zero_base_size_gives_zero():
    d = decide_size(0.0, 0.9, CAPS, ENABLED)
    assert d.size == 0.0


def test_size_never_exceeds_any_cap_over_grid():
    # Property check: no combination of inputs can break a hard cap.
    for base, score, regime in itertools.product(
            (0.0, 10.0, 80.0, 500.0), (0.0, 0.5, 0.7, 0.9, 1.0), (0.0, 0.5, 1.0)):
        d = decide_size(base, score, CAPS, ENABLED, regime_factor=regime)
        assert 0.0 <= d.size <= min(CAPS.values()) + 1e-12


def test_audit_record_has_inputs_and_reason():
    d = decide_size(80.0, 0.9, CAPS, ENABLED)
    rec = d.as_audit_record()
    assert rec["conviction_score"] == 0.9
    assert rec["caps"] == CAPS
    assert rec["binding"] == "kelly"
    assert rec["size"] == pytest.approx(100.0)


@pytest.mark.parametrize("kwargs", [
    {"base_size": -1.0},
    {"base_size": float("inf")},
    {"conviction_score": -0.1},
    {"caps": {}},
    {"caps": {"kelly": -5.0}},
])
def test_bad_inputs_raise(kwargs):
    args = dict(base_size=80.0, conviction_score=0.6, caps=CAPS, table=ENABLED)
    args.update(kwargs)
    with pytest.raises(ValueError):
        decide_size(**args)
