"""Margin-funded delivery rules (owner 2026-11-25): limit, leverage check against the broker's margin. Pure logic.

Flow stage: Risk gate (margin funding), entry product choice and leverage verification
Status: PHASE 2 IMPLEMENTED
"""
import sys
import pathlib

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from quant.risk.margin_entry import (  # noqa: E402
    MTF, MarginSettings, choose_product, settings_from_dict, settings_from_path, verify_leverage,
)

ON = MarginSettings(enabled=True, limit_inr=500_000.0, max_leverage=5.0)


def _reply(margin):
    return {"status": "success", "data": {"total_margin_required": margin}}


# --- settings ----------------------------------------------------------------

def test_only_boolean_true_enables_margin():
    base = {"risk_budget": {"capital_inr": 1_000_000}}
    for value in (None, "true", 1, "yes", False):
        s = settings_from_dict({**base, "margin": {"mtf_enabled": value, "mtf_borrow_pct": 0.3}})
        assert s.enabled is False
    assert settings_from_dict({**base, "margin": {"mtf_enabled": True, "mtf_borrow_pct": 0.3}}).enabled is True


def test_limit_is_share_of_capital():
    s = settings_from_dict({"risk_budget": {"capital_inr": 1_000_000},
                            "margin": {"mtf_enabled": True, "mtf_borrow_pct": 0.30}})
    assert s.limit_inr == pytest.approx(300_000.0)


@pytest.mark.parametrize("pct", [None, 0, -0.1, 1.5, True, "0.3", float("nan")])
def test_bad_borrow_share_means_no_limit(pct):
    s = settings_from_dict({"risk_budget": {"capital_inr": 1_000_000},
                            "margin": {"mtf_enabled": True, "mtf_borrow_pct": pct}})
    assert s.limit_inr is None


@pytest.mark.parametrize("lev, expected", [(5.0, 5.0), (5.5, None), (1.0, None), (0, None), (None, None), (True, None)])
def test_max_leverage_only_between_one_and_five(lev, expected):
    s = settings_from_dict({"margin": {"mtf_enabled": True, "mtf_max_leverage": lev}})
    assert s.max_leverage == expected


def test_unreadable_settings_file_fails_closed(tmp_path):
    bad = tmp_path / "settings.yaml"
    bad.write_text("- just a list\n", encoding="utf-8")
    assert settings_from_path(bad) == MarginSettings()
    assert settings_from_path(tmp_path / "nope.yaml") == MarginSettings()


def test_settings_file_round_trip(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(yaml.safe_dump({"risk_budget": {"capital_inr": 1_000_000},
                                   "margin": {"mtf_enabled": True, "mtf_borrow_pct": 0.3,
                                              "mtf_max_leverage": 5.0}}), encoding="utf-8")
    s = settings_from_path(cfg)
    assert s.enabled and s.limit_inr == pytest.approx(300_000.0) and s.max_leverage == 5.0


# --- product choice (limit and side only; leverage is checked by verify_leverage) ---

def test_buy_within_limit_is_candidate_mtf():
    assert choose_product("CNC", "BUY", 100_000.0, 0.0, ON)[0] == MTF


def test_exact_limit_allowed_one_rupee_over_refused():
    assert choose_product("CNC", "BUY", 100_000.0, 400_000.0, ON)[0] == MTF
    assert choose_product("CNC", "BUY", 100_000.0, 400_001.0, ON)[0] == "CNC"


def test_margin_off_stays_cnc():
    off = MarginSettings(enabled=False, limit_inr=500_000.0, max_leverage=5.0)
    assert choose_product("CNC", "BUY", 1_000.0, 0.0, off) == ("CNC", "margin off")


def test_no_limit_stays_cnc():
    assert choose_product("CNC", "BUY", 1_000.0, 0.0, MarginSettings(enabled=True, limit_inr=None,
                                                                     max_leverage=5.0))[0] == "CNC"


def test_short_entry_never_margin():
    assert choose_product("CNC", "SELL", 1_000.0, 0.0, ON)[0] == "CNC"


def test_intraday_product_is_never_changed():
    assert choose_product("MIS", "BUY", 1_000.0, 0.0, ON) == ("MIS", "not a delivery entry: margin not used")


def test_missing_settings_object_stays_cnc():
    assert choose_product("CNC", "BUY", 1_000.0, 0.0, None)[0] == "CNC"


def test_negative_borrowed_amount_refused_as_cnc():
    assert choose_product("CNC", "BUY", 1_000.0, -5.0, ON)[0] == "CNC"


# --- leverage verification against the broker's margin --------------------------

def test_margin_at_fifth_of_value_is_five_x_and_allowed():
    ok, reason = verify_leverage(_reply(20_000.0), 100_000.0, 5.0)
    assert ok is True and "5.00x" in reason


def test_margin_below_fifth_implies_leverage_above_max_refused():
    ok, reason = verify_leverage(_reply(10_000.0), 100_000.0, 5.0)   # 10x
    assert ok is False and "above the maximum" in reason


def test_margin_equal_to_full_value_means_no_funding_refused():
    assert verify_leverage(_reply(100_000.0), 100_000.0, 5.0)[0] is False


def test_margin_above_full_value_refused():
    assert verify_leverage(_reply(120_000.0), 100_000.0, 5.0)[0] is False


@pytest.mark.parametrize("reply", [
    None, "ok", {}, {"status": "error"}, {"status": "success"}, {"status": "success", "data": None},
    {"status": "success", "data": {"total_margin_required": None}},
    {"status": "success", "data": {"total_margin_required": 0}},
    {"status": "success", "data": {"total_margin_required": -5}},
    {"status": "success", "data": {"total_margin_required": float("nan")}},
    {"status": "success", "data": {"total_margin_required": True}},
])
def test_unusable_broker_reply_refused(reply):
    assert verify_leverage(reply, 100_000.0, 5.0)[0] is False


def test_no_max_leverage_refuses_everything():
    assert verify_leverage(_reply(20_000.0), 100_000.0, None)[0] is False


@pytest.mark.parametrize("value", [0, -1, float("nan"), None])
def test_unusable_order_value_refused(value):
    assert verify_leverage(_reply(20_000.0), value, 5.0)[0] is False
