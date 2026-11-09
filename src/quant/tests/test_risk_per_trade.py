"""Risk per trade (owner decision 0020, updated 2026-11-08 to 0.5%). Helper and loader."""
import pathlib

import pytest

from quant.core.config import RiskLimits
from quant.portfolio.sizing import risk_budget_qty

ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_budget_is_half_percent_of_equity_over_stop_distance():
    # 1,000,000 * 0.5% = 5,000 INR at risk. Stop 2 INR away -> 2,500 shares.
    # (Was 0.25% until the owner raised it on 2026-11-08.)
    assert risk_budget_qty(1_000_000, 0.005, entry=100.0, stop=98.0) == 2500


def test_rounds_down_to_whole_lots():
    # 2500 shares rounds down to 2500 in lots of 100 exactly.
    assert risk_budget_qty(1_000_000, 0.005, entry=100.0, stop=98.0, lot_size=100) == 2500


@pytest.mark.parametrize("equity,frac,entry,stop", [
    (0, 0.0025, 100.0, 98.0), (1e6, 0.0, 100.0, 98.0), (1e6, 0.0025, 100.0, 100.0),
    (float("nan"), 0.0025, 100.0, 98.0), (1e6, 0.0025, float("inf"), 98.0),
])
def test_bad_inputs_give_zero(equity, frac, entry, stop):
    assert risk_budget_qty(equity, frac, entry, stop) == 0


def test_repo_setting_is_half_percent():
    """The committed settings.yaml carries the owner's 2026-11-08 value of 0.5%."""
    assert RiskLimits.risk_per_trade(ROOT / "config" / "settings.yaml") == 0.005


def test_missing_file_or_key_gives_default(tmp_path):
    assert RiskLimits.risk_per_trade(tmp_path / "none.yaml") == 0.0025
    p = tmp_path / "s.yaml"
    p.write_text("mode: paper\n", encoding="utf-8")
    assert RiskLimits.risk_per_trade(p) == 0.0025


def test_custom_value_is_read(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("sizing:\n  risk_per_trade: 0.01\n", encoding="utf-8")
    assert RiskLimits.risk_per_trade(p) == 0.01


@pytest.mark.parametrize("bad", ["0", "0.2", "-0.001", "'high'"])
def test_out_of_range_value_is_refused(tmp_path, bad):
    p = tmp_path / "s.yaml"
    p.write_text(f"sizing:\n  risk_per_trade: {bad}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        RiskLimits.risk_per_trade(p)
