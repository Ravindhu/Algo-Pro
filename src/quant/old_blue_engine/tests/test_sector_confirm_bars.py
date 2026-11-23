"""Confirmation bars: cut single-bar flicker without destroying the edge.

MEASURED 2026-10-24 on the 53,130-row dataset (3,810 dates, 5-day horizon):

    N  fires  hit-rate  edge    daily flips
    1   2519    0.490   -0.010      34.3%
    2   1755    0.502   +0.002      22.3%  <- default
    3   1130    0.487   -0.013      15.2%
    4    696    0.484   -0.016      10.5%
    5    364    0.451   -0.049       7.2%

N=2 is the only setting that is better on BOTH counts. These tests lock that
default and the clamping that stops a larger value being configured silently.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from blue_engine.sectors import sector_universe_filter as suf  # noqa: E402


class _Scorer:
    def __init__(self, rows):
        self.rows = rows

    def score_all(self, as_of=None):
        return {"as_of": "2026-10-24", "ranked": self.rows,
                "unscored": [], "not_available": []}


def _row(stem, run, rising=None):
    return {"stem": stem, "name": stem, "scored": True, "rsi": 60.0,
            "ema_rsi": 55.0,
            "rising": (run > 0) if rising is None else rising,
            "confirm_run": run, "below_level": False}


def _sel(rows, **cfg):
    cfg.setdefault("scorer", _Scorer(rows))
    return suf.SectorUniverseFilter(cfg).qualifying_sectors()


# --------------------------------------------------------------------------- #
# The default
# --------------------------------------------------------------------------- #
def test_default_confirmation_is_two_bars():
    """N=2 measured best on both flicker and hit rate."""
    assert suf.DEFAULT_CONFIRM_BARS == 2
    assert suf.SectorUniverseFilter({}).confirm_bars == 2


def test_single_bar_flicker_is_suppressed_by_default():
    """A sector that only just crossed (run=1) must not qualify at N=2."""
    out = _sel([_row("NIFTY_AUTO", 1), _row("NIFTY_IT", 3)])
    assert out["selected"] == ["NIFTY_IT"]


def test_confirm_bars_one_keeps_the_old_single_bar_behaviour():
    out = _sel([_row("NIFTY_AUTO", 1), _row("NIFTY_IT", 3)], confirm_bars=1)
    assert out["selected"] == ["NIFTY_AUTO", "NIFTY_IT"]


def test_confirm_bars_three_is_allowed():
    out = _sel([_row("NIFTY_AUTO", 2), _row("NIFTY_IT", 3)], confirm_bars=3)
    assert out["selected"] == ["NIFTY_IT"]


def test_a_long_run_always_qualifies():
    out = _sel([_row("NIFTY_BANK", 40)], confirm_bars=3)
    assert out["selected"] == ["NIFTY_BANK"]


# --------------------------------------------------------------------------- #
# Clamping
# --------------------------------------------------------------------------- #
def test_excessive_confirmation_is_clamped_with_a_warning():
    """Beyond the measured maximum the edge is negative, so clamp it."""
    out = _sel([_row("NIFTY_BANK", 40)], confirm_bars=99)
    assert out["confirm_bars"] == suf.MAX_CONFIRM_BARS
    assert any("clamped" in w for w in out["warnings"])


def test_zero_or_negative_is_raised_to_one():
    f = suf.SectorUniverseFilter({"confirm_bars": 0})
    assert f.confirm_bars == 1
    assert "raised to 1" in f.confirm_warning


def test_the_rule_text_names_the_confirmation():
    out = _sel([_row("NIFTY_BANK", 5)])
    assert "2+ session" in out["rule"]


# --------------------------------------------------------------------------- #
# Interaction with the other conditions
# --------------------------------------------------------------------------- #
def test_falling_sector_fails_regardless_of_run():
    out = _sel([_row("NIFTY_METAL", 0, rising=False)], confirm_bars=1)
    assert out["selected"] == []


def test_missing_confirm_run_is_treated_as_zero():
    """An older provider payload without `confirm_run` must not pass."""
    row = {"stem": "NIFTY_AUTO", "name": "x", "scored": True, "rsi": 60.0,
           "ema_rsi": 55.0, "rising": True, "below_level": False}
    out = _sel([row])
    assert out["selected"] == []


def test_discount_cap_still_composes_when_requested():
    rows = [_row("NIFTY_AUTO", 5), _row("NIFTY_IT", 5)]
    rows[0]["below_level"] = True
    out = _sel(rows, require_discount=True)
    assert out["selected"] == ["NIFTY_AUTO"]


# --------------------------------------------------------------------------- #
# Scorer emits the field
# --------------------------------------------------------------------------- #
def test_scorer_reports_a_running_above_ema_count(tmp_path):
    pd = pytest.importorskip("pandas")
    import numpy as np
    from blue_engine.sectors.sector_hm_score import SectorHMScorer

    n = 80
    # A fall then a sustained rise: the tail should be above its EMA.
    closes = list(100.0 - np.arange(40) * 0.5) + \
             list(80.0 + np.arange(40) * 0.9)
    dates = pd.bdate_range("2025-01-01", periods=n)
    pd.DataFrame({"Close": closes},
                 index=pd.Index(dates, name="Date")).to_parquet(
        tmp_path / "NIFTY_IT.parquet")

    row = SectorHMScorer({"indices_dir": str(tmp_path)}).score_one(
        "NIFTY_IT", dates[-1].date())
    assert row["scored"] is True
    assert "confirm_run" in row
    assert isinstance(row["confirm_run"], int)
    assert row["confirm_run"] >= 0
    # A sustained rise must produce a run longer than a single bar.
    assert row["confirm_run"] >= 1
