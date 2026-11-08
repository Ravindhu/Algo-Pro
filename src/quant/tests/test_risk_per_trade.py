"""Risk per trade = 0.25% of equity (owner decision 0020). Helper and settings loader."""
import pathlib

import pytest

from quant.core.config import RiskLimits
from quant.portfolio.sizing import risk_budget_qty

ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_budget_is_quarter_percent_of_equity_over_stop_distance():
    # 1,000,000 * 0.25% = 2,500 INR at risk. Stop 2 INR away -> 1,250 shares.
    assert risk_budget_qty(1_000_000, 0.0025, entry=100.0, stop=98.0) == 1250


def test_rounds_down_to_whole_lots():
    # 1250 shares rounds down to 1200 in lots of 100.
    assert risk_budget_qty(1_000_000, 0.0025, entry=100.0, stop=98.0, lot_size=100) == 1200


@pytest.mark.parametrize("equity,frac,entry,stop", [
    (0, 0.0025, 100.0, 98.0), (1e6, 0.0, 100.0, 98.0), (1e6, 0.0025, 100.0, 100.0),
    (float("nan"), 0.0025, 100.0, 98.0), (1e6, 0.0025, float("inf"), 98.0),
])
def test_bad_inputs_give_zero(equity, frac, entry, stop):
    assert risk_budget_qty(equity, frac, entry, stop) == 0


def test_repo_setting_is_quarter_percent():
    assert RiskLimits.risk_per_trade(ROOT / "config" / "settings.yaml") == 0.0025


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
