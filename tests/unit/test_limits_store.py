"""User-set risk limits: validation and atomic save (owner decisions 0020, 0031). No FastAPI needed."""
import pytest
import yaml

from quant.core.config import RiskLimits
from quant.core.limits_store import apply_limit_updates, validate_limit_updates


def _file(tmp_path):
    p = tmp_path / "risk_limits.yaml"
    p.write_text("blocked: false\nmax_order_quantity: null\ncapital_inr: 1000000\nmax_daily_loss_pct: null\n",
                 encoding="utf-8")
    return p


def test_valid_update_is_saved_and_reported(tmp_path):
    p = _file(tmp_path)
    r = apply_limit_updates(p, {"max_daily_loss_pct": 0.005, "max_order_quantity": 10})
    assert r == {"old": {"max_daily_loss_pct": None, "max_order_quantity": None},
                 "new": {"max_daily_loss_pct": 0.005, "max_order_quantity": 10}}
    limits = RiskLimits.from_yaml(p)
    assert limits.max_daily_loss == 5000.0 and limits.max_order_quantity == 10


def test_null_clears_a_limit(tmp_path):
    p = _file(tmp_path)
    apply_limit_updates(p, {"max_daily_loss_pct": 0.005})
    apply_limit_updates(p, {"max_daily_loss_pct": None})
    assert RiskLimits.from_yaml(p).max_daily_loss is None


def test_blocked_flag_round_trips(tmp_path):
    p = _file(tmp_path)
    apply_limit_updates(p, {"blocked": True})
    assert RiskLimits.from_yaml(p).blocked is True


def test_header_comment_is_written(tmp_path):
    p = _file(tmp_path)
    apply_limit_updates(p, {"max_daily_loss_pct": 0.005})
    assert p.read_text(encoding="utf-8").startswith("# Hard limits.")


def test_saved_file_has_no_rupee_keys(tmp_path):
    p = _file(tmp_path)
    p.write_text("blocked: false\nmax_daily_loss: 1000\n", encoding="utf-8")  # old-style file
    apply_limit_updates(p, {"max_daily_loss_pct": 0.005})
    text = p.read_text(encoding="utf-8")
    assert "max_daily_loss:" not in text and "max_daily_loss_pct" in text


def test_no_temp_file_left_behind(tmp_path):
    p = _file(tmp_path)
    apply_limit_updates(p, {"max_daily_loss_pct": 0.005})
    assert not (tmp_path / "risk_limits.yaml.tmp").exists()


@pytest.mark.parametrize("bad", [
    {}, {"unknown_limit": 5}, {"max_daily_loss_pct": -0.1}, {"max_daily_loss_pct": "0.5"},
    {"max_daily_loss_pct": True}, {"max_daily_loss_pct": 1.5}, {"max_adv_pct": 1.5},
    {"max_order_quantity": 2.5}, {"blocked": "yes"}, {"max_daily_loss_pct": float("nan")},
    {"capital_inr": 0},
    {"max_daily_loss": 1000},  # rupee key is retired
    {"max_strategy_loss": 500},
])
def test_bad_updates_are_refused_and_file_is_unchanged(tmp_path, bad):
    p = _file(tmp_path)
    before = p.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        apply_limit_updates(p, bad)
    assert p.read_text(encoding="utf-8") == before


def test_whole_number_float_is_accepted_for_quantity():
    assert validate_limit_updates({"max_order_quantity": 3.0}) == {"max_order_quantity": 3}


def test_repo_limits_file_still_loads_with_all_null_defaults():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    limits = RiskLimits.from_yaml(root / "config" / "risk_limits.yaml")
    assert isinstance(limits, RiskLimits)
    assert yaml.safe_load((root / "config" / "risk_limits.yaml").read_text(encoding="utf-8")) is not None
