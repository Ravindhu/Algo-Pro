"""risk_utilisation = open exposure / max_open_exposure. 0.0 when the limit is unset (decision 0020, D7)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from quant.reporting.dashboard_data import build_overview, risk_utilisation  # noqa: E402


def _open(qty, avg):
    return {"S": {"qty": qty, "avg_price": avg}}


def test_exposure_share_of_limit():
    # 10 shares x 100 = 1000 exposure; limit 20000 -> 5%
    assert risk_utilisation(_open(10, 100.0), 20000) == 0.05


def test_short_position_counts_as_exposure():
    assert risk_utilisation(_open(-10, 100.0), 20000) == 0.05


def test_unset_limit_gives_zero():
    assert risk_utilisation(_open(10, 100.0), None) == 0.0


def test_no_positions_gives_zero():
    assert risk_utilisation({}, 20000) == 0.0


def test_overview_uses_ledger_exposure_by_default():
    fills = [{"symbol": "A", "signed_qty": 10, "price": 100.0}]
    o = build_overview(fills=fills, audit_records=[], kill={}, limits={"max_open_exposure": 20000},
                       last_prices={})
    assert o["performance"]["risk_utilisation"] == 0.05


def test_explicit_risk_used_still_wins():
    o = build_overview(fills=[], audit_records=[], kill={}, limits={"max_open_exposure": None},
                       last_prices={}, risk_used=0.3)
    assert o["performance"]["risk_utilisation"] == 0.3
