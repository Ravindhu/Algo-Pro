"""Margin funding (MTF) risk check: off by default, refuses without a limit, fits the limit exactly.

Flow stage: Risk gate (margin funding)
Status: PHASE 1 IMPLEMENTED (pure check; no broker path)
"""
import pytest
import yaml

from quant.core.setup_store import load_mtf_enabled
from quant.execution.openalgo_client import _order_product
from quant.research.costs import CostModel
from quant.risk.margin_funding import margin_exposure_check


def test_off_refuses_even_with_a_limit():
    ok, reason = margin_exposure_check(False, 100000.0, 0.0, 1000.0)
    assert ok is False and "off" in reason


@pytest.mark.parametrize("enabled", ["yes", 1, None])
def test_only_boolean_true_enables(enabled):
    ok, _ = margin_exposure_check(enabled, 100000.0, 0.0, 1000.0)
    assert ok is False


@pytest.mark.parametrize("limit", [None, 0, -5, float("nan"), True])
def test_no_usable_limit_refuses(limit):
    ok, reason = margin_exposure_check(True, limit, 0.0, 1000.0)
    assert ok is False and "limit" in reason


def test_within_limit_is_allowed():
    assert margin_exposure_check(True, 100000.0, 40000.0, 60000.0) == (True, "ok")


def test_exactly_at_limit_is_allowed_one_paisa_over_is_refused():
    assert margin_exposure_check(True, 100000.0, 40000.0, 60000.0)[0] is True
    ok, reason = margin_exposure_check(True, 100000.0, 40000.0, 60000.01)
    assert ok is False and "above the limit" in reason


@pytest.mark.parametrize("borrowed, new", [(-1.0, 10.0), (10.0, float("inf")), (True, 10.0)])
def test_bad_amounts_refused(borrowed, new):
    ok, reason = margin_exposure_check(True, 100000.0, borrowed, new)
    assert ok is False and "bad borrowed" in reason


def test_daily_interest_uses_the_existing_cost_model():
    assert CostModel().mtf_daily_interest(100000.0) == pytest.approx(100000.0 * 0.12 / 365)


def test_setting_defaults_to_off(tmp_path):
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump({"margin": {"mtf_borrowed_inr": 500000}}), encoding="utf-8")
    assert load_mtf_enabled(settings) is False
    settings.write_text(yaml.safe_dump({"margin": {"mtf_enabled": True}}), encoding="utf-8")
    assert load_mtf_enabled(settings) is True


def test_order_client_accepts_margin_product_only_as_mtf():
    # owner 2026-11-25: MTF is the margin-delivery order product (mapped to mStock MARGIN)
    assert _order_product("mtf") == "MTF"
    with pytest.raises(ValueError):
        _order_product("MARGIN")

