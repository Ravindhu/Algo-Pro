"""Broker brokerage setup: product x method, both charge calculations.

Owner request 2026-11-18: each broker charges differently, so the trade setup
must hold BOTH a fixed rupee fee and a bps rate, and the code must compute both.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "quant"))

from quant.core import broker_costs as bc  # noqa: E402
from quant.core import setup_store  # noqa: E402


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def test_fixed_method_needs_the_fee():
    with pytest.raises(ValueError, match="fixed_per_order"):
        bc.validate_brokerage({"method": "fixed"})


def test_bps_method_needs_the_rate():
    with pytest.raises(ValueError, match="brokerage.bps"):
        bc.validate_brokerage({"method": "bps"})


def test_bad_product_and_method_are_refused():
    with pytest.raises(ValueError, match="product"):
        bc.validate_brokerage({"product": "futures", "method": "bps", "bps": 3})
    with pytest.raises(ValueError, match="method"):
        bc.validate_brokerage({"method": "percent", "bps": 3})


def test_negative_or_zero_fee_is_refused():
    with pytest.raises(ValueError):
        bc.validate_brokerage({"method": "fixed", "fixed_per_order": 0})
    with pytest.raises(ValueError):
        bc.validate_brokerage({"method": "fixed", "fixed_per_order": -5})


# --------------------------------------------------------------------------- #
# BOTH calculations, on the same notional
# --------------------------------------------------------------------------- #
def test_fixed_charge_is_the_same_rupees_on_any_ticket():
    setup = bc.validate_brokerage({"method": "fixed", "fixed_per_order": 20})
    assert bc.brokerage_charge(2_000.0, setup) == pytest.approx(20.0)
    assert bc.brokerage_charge(200_000.0, setup) == pytest.approx(20.0)


def test_bps_charge_scales_with_the_ticket():
    setup = bc.validate_brokerage({"method": "bps", "bps": 3})
    assert bc.brokerage_charge(10_000.0, setup) == pytest.approx(3.0)
    assert bc.brokerage_charge(100_000.0, setup) == pytest.approx(30.0)


def test_small_ticket_costs_more_in_bps_under_a_flat_fee():
    """The whole reason both methods exist: one number cannot express both."""
    fixed = bc.validate_brokerage({"method": "fixed", "fixed_per_order": 20})
    small = bc.brokerage_charge(2_000.0, fixed) / 2_000.0 * 10000.0
    large = bc.brokerage_charge(200_000.0, fixed) / 200_000.0 * 10000.0
    assert small == pytest.approx(100.0)
    assert large == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# product: STT follows delivery or intraday
# --------------------------------------------------------------------------- #
def test_stt_is_delivery_by_default_and_lower_for_intraday():
    assert bc.stt_charge(10_000.0, "delivery") == pytest.approx(10.0)
    assert bc.stt_charge(10_000.0, "intraday") == pytest.approx(2.5)


# --------------------------------------------------------------------------- #
# the CostModel block both methods map onto (decision 0037)
# --------------------------------------------------------------------------- #
def test_fixed_method_maps_to_flat_fee_and_zeroes_the_bps_rate():
    cfg = bc.cost_config({"method": "fixed", "fixed_per_order": 20,
                          "product": "delivery"})
    assert cfg["flat_brokerage_per_order"] == 20
    assert cfg["brokerage"] == 0.0
    assert cfg["stt"] == 10.0


def test_bps_method_maps_to_the_bps_rate():
    cfg = bc.cost_config({"method": "bps", "bps": 4, "product": "intraday"})
    assert "flat_brokerage_per_order" not in cfg
    assert cfg["brokerage"] == 4.0
    assert cfg["stt"] == 2.5


# --------------------------------------------------------------------------- #
# setup store wiring
# --------------------------------------------------------------------------- #
@pytest.fixture()
def setup_env(tmp_path):
    settings = tmp_path / "settings.yaml"
    limits = tmp_path / "risk_limits.yaml"
    limits.write_text("max_symbols_per_portfolio: 5\n", encoding="utf-8")
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (strategies / "ocl.yaml").write_text("name: ocl\n", encoding="utf-8")
    return settings, strategies, limits


def test_setup_round_trips_both_methods(setup_env):
    settings, strategies, limits = setup_env
    fixed = bc.validate_brokerage({"method": "fixed", "fixed_per_order": 20,
                                   "product": "delivery"})
    setup_store.apply_setup_updates(settings, strategies, limits,
                                    {"strategy": "ocl", "brokerage": fixed})
    read = setup_store.read_setup(settings, strategies, limits)
    assert read["brokerage"]["method"] == "fixed"
    assert read["brokerage"]["fixed_per_order"] == 20
    assert read["brokerage_example"]["brokerage"] == 20.0

    bps = bc.validate_brokerage({"method": "bps", "bps": 2.5,
                                 "product": "intraday"})
    setup_store.apply_setup_updates(settings, strategies, limits,
                                    {"brokerage": bps})
    read = setup_store.read_setup(settings, strategies, limits)
    assert read["brokerage"]["method"] == "bps"
    assert read["brokerage"]["bps"] == 2.5
    assert read["brokerage"]["product"] == "intraday"
    # Rs 10,000 example: 2.5 brokerage + 2.5 STT per side = 10 round trip.
    assert read["brokerage_example"]["round_trip"] == pytest.approx(10.0)


def test_unset_brokerage_reads_as_null(setup_env):
    settings, strategies, limits = setup_env
    read = setup_store.read_setup(settings, strategies, limits)
    assert read["brokerage"] is None
    assert read["brokerage_example"] is None


def test_bad_brokerage_is_refused_at_the_api_boundary(setup_env):
    settings, strategies, limits = setup_env
    with pytest.raises(ValueError, match="fixed_per_order"):
        setup_store.apply_setup_updates(settings, strategies, limits,
                                        {"brokerage": {"method": "fixed"}})


def test_settings_round_trip_is_atomic_and_keeps_other_keys(setup_env):
    settings, strategies, limits = setup_env
    settings.write_text("other: keep\n", encoding="utf-8")
    setup = bc.validate_brokerage({"method": "bps", "bps": 3})
    setup_store.apply_setup_updates(settings, strategies, limits,
                                    {"brokerage": setup})
    import yaml
    data = yaml.safe_load(settings.read_text(encoding="utf-8"))
    assert data["other"] == "keep"
    assert data["trade_setup"]["brokerage"]["bps"] == 3
