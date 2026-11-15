"""Band POV mode (2026-11-15): high of band -> lower POV, low of band -> higher POV.

Fixed mode is the default and must match the old behaviour exactly.
"""
from types import SimpleNamespace

import pandas as pd
import pytest

from quant.execution.pov_executor import PovExecutor
from quant.execution.strategy_bot import BotConfig, StrategyBot


def _df(ltp: float) -> pd.DataFrame:
    # iloc[-2] is the last closed bar, so put the price there.
    return pd.DataFrame({"close": [ltp, ltp, ltp]})


def _pov(cfg: BotConfig, vwap, ltp: float, side: str = "BUY"):
    fake = SimpleNamespace(cfg=cfg, _vwap_provider=(lambda sym: vwap), _log=lambda *a, **k: None)
    return StrategyBot._pov_for(fake, "TEST", _df(ltp), side)


def test_fixed_is_default_and_uses_cfg_pov():
    cfg = BotConfig()
    assert cfg.pov_mode == "fixed"
    fake = SimpleNamespace(cfg=cfg, _vwap_provider=None, _log=lambda *a, **k: None)
    assert StrategyBot._pov_for(fake, "TEST", _df(100.0), "BUY") == (cfg.pov, "fixed")


def test_band_high_gives_lower_pov_than_low():
    cfg = BotConfig(pov_mode="band")
    near_ceiling, _ = _pov(cfg, vwap=100.0, ltp=100.4)   # high of band
    near_floor, _ = _pov(cfg, vwap=100.0, ltp=97.1)      # low of band
    assert 0 < near_ceiling < near_floor <= 0.25
    assert near_ceiling >= 0.05


def test_band_above_ceiling_halts():
    rate, reason = _pov(BotConfig(pov_mode="band"), vwap=100.0, ltp=101.0)
    assert rate == 0.0
    assert reason == "halt"


def test_band_below_floor_uses_fixed_pov_as_passthrough():
    cfg = BotConfig(pov=0.1, pov_mode="band")
    rate, reason = _pov(cfg, vwap=100.0, ltp=96.0)
    assert reason == "passthrough"
    assert rate == pytest.approx(0.1)


def test_band_without_verified_vwap_falls_back_to_fixed():
    cfg = BotConfig(pov=0.1, pov_mode="band")
    rate, reason = _pov(cfg, vwap=None, ltp=100.0)
    assert (rate, reason) == (0.1, "fallback")


@pytest.mark.parametrize("bad", [
    {"pov_mode": "vwap"},
    {"pov_mode": "band", "pov_band": {"unknown": 1}},
    {"pov_mode": "band", "pov_band": {"min_pov": 0.3, "max_pov": 0.1}},
    {"pov_mode": "band", "pov_band": {"max_pov": 1.5}},
    {"pov_mode": "band", "pov_band": {"passthrough_floor": "zero"}},
    {"pov_mode": "band", "pov_band": {"vwap_floor_pct": 0}},
])
def test_bad_band_config_is_refused(bad):
    with pytest.raises(ValueError):
        BotConfig(**bad)


def test_child_qty_uses_passed_pov_and_default_is_unchanged():
    ex = PovExecutor.__new__(PovExecutor)
    ex.pov = 0.1
    # 10% of 1000 = 100, capped at remaining 50
    assert ex.child_qty(50, 1000.0) == ex.child_qty(50, 1000.0, None) == 50
    assert ex.child_qty(500, 1000.0, 0.2) == 200
    assert ex.child_qty(500, 1000.0) == 100
