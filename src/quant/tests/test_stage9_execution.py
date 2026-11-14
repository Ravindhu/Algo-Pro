"""Stage 9 (Execution): router sizing, TCA, and the order-path rule.

The order-path rule is the single most important invariant in the system: only
`execution/openalgo_client.py` may name a broker order endpoint. It is checked
here as well as in test_layout.py, because eight stages of edits have happened
since it was last verified.
"""
import pathlib
import re

import pytest

from quant.execution.router import pov_child_qty, pov_child_qty_report, vwap_schedule
from quant.monitor.tca import (calibrated_slippage_bps, mean_slippage_bps,
                               slippage_bps, tca_report)

ROOT = pathlib.Path(__file__).resolve().parents[3]

#: The one file allowed to reach a broker's order endpoints.
ORDER_PATH_FILE = "openalgo_client.py"

#: Directories the rule protects. The engine and the vendored platform are out of
#: scope: they are upstream code, wrapped not authored here.
PROTECTED = ("risk", "portfolio", "strategies", "models", "monitor", "execution")


# --------------------------------------------------------------------------- #
# THE ORDER-PATH RULE
# --------------------------------------------------------------------------- #
def test_only_the_order_client_names_placeorder():
    """No module outside openalgo_client.py may call a broker order endpoint."""
    offenders = []
    for lane in PROTECTED:
        folder = ROOT / "src" / "quant" / lane
        for path in folder.rglob("*.py"):
            if path.name == ORDER_PATH_FILE:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                if re.search(r"\.(placeorder|placesmartorder|cancelorder|modifyorder)\s*\(", line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert offenders == [], "order endpoint called outside the order client: " + ", ".join(offenders)


def test_no_llm_import_in_the_risk_lane():
    """AGENTS.md: no LLM client in risk/, execution/, portfolio/ or monitor/ledger.py."""
    offenders = []
    for lane in ("risk", "execution", "portfolio"):
        for path in (ROOT / "src" / "quant" / lane).rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if re.search(r"^\s*(import|from)\s+(openai|anthropic|ollama)\b", text, re.M):
                offenders.append(str(path.relative_to(ROOT)))
    ledger = ROOT / "src" / "quant" / "monitor" / "ledger.py"
    if ledger.is_file():
        text = ledger.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^\s*(import|from)\s+(openai|anthropic|ollama)\b", text, re.M):
            offenders.append(str(ledger.relative_to(ROOT)))
    assert offenders == []


# --------------------------------------------------------------------------- #
# POV child sizing
# --------------------------------------------------------------------------- #
def test_pov_sizes_a_fraction_of_the_bar():
    assert pov_child_qty(1000, 5000, 0.1) == 500


def test_pov_never_exceeds_the_remainder():
    assert pov_child_qty(100, 5000, 0.1) == 100


def test_negative_pov_is_refused():
    """It used to return a NEGATIVE quantity: a child in the wrong direction."""
    with pytest.raises(ValueError, match="pov must be > 0"):
        pov_child_qty(1000, 5000, -0.5)


def test_zero_pov_is_refused():
    with pytest.raises(ValueError, match="pov must be > 0"):
        pov_child_qty(1000, 5000, 0.0)


def test_nan_pov_is_refused_clearly():
    with pytest.raises(ValueError, match="finite"):
        pov_child_qty(1000, 5000, float("nan"))


def test_percent_form_is_accepted():
    assert pov_child_qty(1000, 5000, 10) == 500


def test_thin_bar_is_a_stall_not_an_exception():
    qty, note = pov_child_qty_report(1000, 5, 0.1)
    assert qty == 0
    assert "stall" in note


def test_stall_is_named_so_a_caller_can_alert():
    """A silent zero means the parent never completes and nobody notices."""
    _, note = pov_child_qty_report(1000, 5, 0.1)
    assert "raise POV" in note or "fuller bar" in note


def test_normal_child_has_no_note():
    qty, note = pov_child_qty_report(1000, 5000, 0.1)
    assert qty == 500 and note == ""


def test_complete_parent_reports_complete():
    qty, note = pov_child_qty_report(0, 5000, 0.1)
    assert qty == 0 and note == "parent complete"


# --------------------------------------------------------------------------- #
# VWAP schedule
# --------------------------------------------------------------------------- #
def test_vwap_preserves_the_total():
    for total in (0, 1, 100, 1001):
        assert sum(vwap_schedule(total, [0.3, 0.3, 0.4])) == total


def test_empty_profile_is_refused():
    """It used to raise IndexError on out[-1]."""
    with pytest.raises(ValueError, match="empty"):
        vwap_schedule(100, [])


def test_zero_sum_profile_is_refused():
    with pytest.raises(ValueError, match="sums to zero"):
        vwap_schedule(100, [0.0, 0.0])


def test_negative_weight_is_refused():
    with pytest.raises(ValueError, match=">= 0"):
        vwap_schedule(100, [0.5, -0.1])


def test_negative_total_is_refused():
    with pytest.raises(ValueError, match=">= 0"):
        vwap_schedule(-5, [0.5])


# --------------------------------------------------------------------------- #
# TCA
# --------------------------------------------------------------------------- #
def test_buy_above_arrival_is_a_cost():
    assert slippage_bps("BUY", 100.5, 100.0) == pytest.approx(50.0)


def test_sell_below_arrival_is_a_cost():
    assert slippage_bps("SELL", 99.5, 100.0) == pytest.approx(50.0)


def test_buy_below_arrival_is_a_gain():
    assert slippage_bps("BUY", 99.5, 100.0) < 0


def test_zero_arrival_returns_nan_not_an_exception():
    import math
    assert math.isnan(slippage_bps("BUY", 100.0, 0.0))


def test_nan_fill_returns_nan():
    import math
    assert math.isnan(slippage_bps("BUY", float("nan"), 100.0))


def test_unknown_side_is_refused():
    """It used to be treated as SELL, flipping the sign: a cost looked like a gain."""
    with pytest.raises(ValueError, match="BUY or SELL"):
        slippage_bps("SHORT", 101.0, 100.0)


def test_mean_survives_a_bad_row():
    """One NaN row used to poison the whole period's average."""
    rows = [{"side": "BUY", "fill": float("nan"), "arrival": 100.0},
            {"side": "BUY", "fill": 101.0, "arrival": 100.0}]
    assert mean_slippage_bps(rows) == pytest.approx(100.0)


def test_report_counts_usable_rows():
    rows = [{"side": "BUY", "fill": 100.5, "arrival": 100.0},
            {"side": "BUY", "fill": float("nan"), "arrival": 100.0}]
    rep = tca_report(rows)
    assert rep["n_total"] == 2
    assert rep["n_usable"] == 1


def test_calibration_needs_enough_samples():
    """Fitting a cost model on three fills is fitting noise."""
    rows = [{"side": "BUY", "fill": 100.5, "arrival": 100.0}] * 3
    out = calibrated_slippage_bps(rows, assumed_bps=5.0)
    assert out["applied"] is False
    assert out["new_bps"] == 5.0


def test_calibration_blends_toward_the_measurement():
    rows = [{"side": "BUY", "fill": 100.5, "arrival": 100.0}] * 30
    out = calibrated_slippage_bps(rows, assumed_bps=5.0, blend=0.5)
    assert out["applied"] is True
    assert 5.0 < out["new_bps"] < 50.0


def test_calibration_never_goes_negative():
    """A measured gain must not become a credit that flatters every backtest."""
    rows = [{"side": "BUY", "fill": 99.5, "arrival": 100.0}] * 30
    out = calibrated_slippage_bps(rows, assumed_bps=5.0, blend=1.0)
    assert out["new_bps"] >= 0.0
