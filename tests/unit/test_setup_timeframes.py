"""Trade setup timeframes (HTF, MTF, LTF): common to all strategies.

Owner request 2026-11-18. Values are saved under trade_setup.timeframes in
config/settings.yaml. Rule: LTF < MTF < HTF in minutes when all three are set.
Plan: docs/IMPLEMENTATION_PLAN_2026-11-18_TIMEFRAMES.md.
"""
from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "quant"))

from quant.core import setup_store as ss  # noqa: E402


@pytest.fixture
def paths(tmp_path):
    settings = tmp_path / "settings.yaml"
    settings.write_text("sizing:\n  risk_per_trade: 0.0025\n", encoding="utf-8")
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (strategies / "ocl.yaml").write_text("name: ocl\n", encoding="utf-8")
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text("max_symbols_per_portfolio: null\n", encoding="utf-8")
    return settings, strategies, limits


def _save(paths, updates):
    settings, strategies, limits = paths
    return ss.apply_setup_updates(settings, strategies, limits, updates)


def _tf(paths):
    data = yaml.safe_load(paths[0].read_text(encoding="utf-8"))
    return (data.get("trade_setup") or {}).get("timeframes")


def test_valid_set_is_saved_and_read_back(paths):
    _save(paths, {"htf_interval": "1d", "mtf_interval": "15m", "ltf_interval": "5m"})
    assert _tf(paths) == {"htf": "1d", "mtf": "15m", "ltf": "5m"}
    read = ss.read_setup(paths[0], paths[1], paths[2])
    assert (read["htf_interval"], read["mtf_interval"], read["ltf_interval"]) == ("1d", "15m", "5m")
    assert read["timeframe_options"] == list(ss.TIMEFRAMES)


def test_unknown_label_is_refused(paths):
    with pytest.raises(ValueError, match="ltf_interval must be one of"):
        ss.validate_setup_updates({"ltf_interval": "2m"}, ["ocl"])


def test_non_string_label_is_refused(paths):
    with pytest.raises(ValueError, match="mtf_interval must be one of"):
        ss.validate_setup_updates({"mtf_interval": ["5m"]}, ["ocl"])


def test_order_must_be_ltf_mtf_htf(paths):
    with pytest.raises(ValueError, match="LTF < MTF < HTF"):
        _save(paths, {"htf_interval": "5m", "mtf_interval": "15m", "ltf_interval": "1m"})
    assert _tf(paths) is None  # refused before any write


def test_equal_timeframes_are_refused(paths):
    with pytest.raises(ValueError, match="LTF < MTF < HTF"):
        _save(paths, {"htf_interval": "15m", "mtf_interval": "15m", "ltf_interval": "5m"})


def test_order_checked_against_stored_values(paths):
    _save(paths, {"htf_interval": "1d", "mtf_interval": "15m", "ltf_interval": "5m"})
    with pytest.raises(ValueError, match="LTF < MTF < HTF"):
        _save(paths, {"ltf_interval": "1h"})  # 1h is above MTF 15m
    assert _tf(paths) == {"htf": "1d", "mtf": "15m", "ltf": "5m"}


def test_null_clears_one_timeframe(paths):
    _save(paths, {"htf_interval": "1d", "mtf_interval": "15m", "ltf_interval": "5m"})
    _save(paths, {"htf_interval": None})
    assert _tf(paths) == {"htf": None, "mtf": "15m", "ltf": "5m"}


def test_partial_set_is_allowed(paths):
    _save(paths, {"ltf_interval": "1m"})
    assert _tf(paths) == {"ltf": "1m"}


def test_old_value_is_recorded_for_audit(paths):
    _save(paths, {"mtf_interval": "5m"})
    result = _save(paths, {"mtf_interval": "15m"})
    assert result["old"]["mtf_interval"] == "5m"
    assert result["new"]["mtf_interval"] == "15m"


def test_other_trade_setup_keys_are_kept(paths):
    _save(paths, {"strategy": "ocl", "brokerage": {"product": "delivery", "method": "bps", "bps": 3}})
    _save(paths, {"htf_interval": "1d", "mtf_interval": "15m", "ltf_interval": "5m"})
    data = yaml.safe_load(paths[0].read_text(encoding="utf-8"))
    assert data["trade_setup"]["strategy"] == "ocl"
    assert data["trade_setup"]["brokerage"]["bps"] == 3
    assert data["sizing"]["risk_per_trade"] == 0.0025
