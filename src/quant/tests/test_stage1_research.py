"""Stage 1 (research validation): data QA gate, capacity, lineage.

These three were the weak links in the stage audit. Each now either wraps the
engine or models the thing the old code approximated linearly.
"""
import math

import pandas as pd
import pytest

from quant.data.qa import audit_bars, check_bars, to_qa_report
from quant.research.capacity import capacity_report, impact_bps, max_capital
from quant.research.lineage import (code_state, read_lineage, record_lineage,
                                    sha256_params, verify_lineage)


# --------------------------------------------------------------------------- #
# Stage 1.2 - Data QA gate
# --------------------------------------------------------------------------- #
def _bars(dates, symbol="A", close=100.0):
    return pd.DataFrame([
        {"date": d, "symbol": symbol, "open": close, "high": close + 1,
         "low": close - 1, "close": close, "volume": 1000}
        for d in dates
    ])


def test_contiguous_bars_pass_the_gap_check():
    rep = check_bars(_bars(["2026-01-01", "2026-01-02", "2026-01-03"]))
    assert rep.passed is True


def test_calendar_gap_is_an_error():
    """A missing fortnight used to pass silently; it must not."""
    rep = check_bars(_bars(["2026-01-01", "2026-01-02", "2026-01-20"]))
    assert rep.passed is False
    assert any("calendar gap" in m for _, m in rep.issues)


def test_gap_within_limit_is_tolerated():
    """A normal long weekend must not be flagged."""
    rep = check_bars(_bars(["2026-01-01", "2026-01-05"]))
    assert not any("calendar gap" in m for _, m in rep.issues)


def test_missing_column_is_still_an_error():
    df = _bars(["2026-01-01"]).drop(columns=["volume"])
    assert check_bars(df).passed is False


def test_duplicate_rows_are_an_error():
    df = pd.concat([_bars(["2026-01-01"]), _bars(["2026-01-01"])])
    assert check_bars(df).passed is False


def test_engine_audit_returns_a_verdict():
    out = audit_bars(_bars(["2026-01-01", "2026-01-02"]),
                     max_stale_days=None, min_rows=1)
    assert out["verdict"] in ("clean", "degraded", "failed")
    assert "summary" in out


def test_engine_audit_fails_on_an_empty_frame():
    empty = _bars([])
    assert audit_bars(empty, min_rows=1)["verdict"] == "failed"


def test_engine_audit_catches_insufficient_history():
    """The engine requires MIN_TRAIN_DAYS; a short sample is not clean."""
    out = audit_bars(_bars(["2026-01-0%d" % i for i in range(1, 6)]),
                     min_rows=750, max_stale_days=None)
    assert out["verdict"] in ("failed", "degraded")


def test_to_qa_report_maps_engine_severity():
    rep = to_qa_report({"verdict": "failed",
                        "fatal": [{"check": "gaps", "detail": "hole"}]})
    assert rep.passed is False


def test_to_qa_report_unavailable_fails_closed():
    rep = to_qa_report({"verdict": "unavailable", "detail": "no engine"})
    assert rep.passed is False


# --------------------------------------------------------------------------- #
# Stage 1.9 - Capacity estimator
# --------------------------------------------------------------------------- #
def test_capacity_is_linear_when_no_edge_given():
    """Back-compatible: the 3-argument call keeps its original meaning."""
    assert max_capital(1_000_000, 0.10, 1) == pytest.approx(100_000)


def test_capacity_scales_with_holding_days():
    assert max_capital(1_000_000, 0.10, 3) == pytest.approx(300_000)


def test_edge_caps_capacity_below_the_adv_rule():
    """A narrow edge must shrink capacity, not be ignored."""
    adv_rule = max_capital(1_000_000, 0.10, 1)
    edge_rule = max_capital(1_000_000, 0.10, 1, edge_bps=0.01, coef=1.0)
    assert edge_rule < adv_rule


def test_zero_edge_means_zero_capacity():
    assert max_capital(1_000_000, 0.10, 1, edge_bps=0.0) == 0.0


def test_impact_law_is_square_root():
    """Doubling participation must NOT double impact."""
    a = impact_bps(100_000, 1_000_000)
    b = impact_bps(200_000, 1_000_000)
    assert b == pytest.approx(a * math.sqrt(2), rel=1e-9)
    assert b < 2 * a


def test_bad_participation_is_refused():
    with pytest.raises(ValueError):
        max_capital(1_000_000, 1.5, 1)


def test_zero_adv_gives_zero_capacity():
    assert max_capital(0, 0.10, 1) == 0.0


def test_capacity_report_shows_the_numbers():
    rep = capacity_report(1_000_000, edge_bps=10)
    assert rep["ceiling"] == "edge"
    assert rep["participation"] > 0
    assert rep["impact_bps_at_limit"] >= 0


# --------------------------------------------------------------------------- #
# Stage 1.10 - Experiment lineage
# --------------------------------------------------------------------------- #
def test_lineage_records_the_run_environment(tmp_path):
    data = tmp_path / "d.csv"
    data.write_text("a,b\n1,2\n", encoding="utf-8")
    row = record_lineage(tmp_path / "l.jsonl", data, "abc", {"p": 1}, "s",
                         dataset="nse", date_range=("2020-01-01", "2026-01-01"),
                         horizon=5, cost_model_version="v2", n_trials=50)
    for key in ("dataset", "date_range", "horizon", "cost_model_version",
                "n_trials", "git_state", "schema_version"):
        assert key in row


def test_lineage_appends_not_overwrites(tmp_path):
    data = tmp_path / "d.csv"
    data.write_text("x\n", encoding="utf-8")
    p = tmp_path / "l.jsonl"
    record_lineage(p, data, "abc", {"p": 1}, "s")
    record_lineage(p, data, "abc", {"p": 2}, "s")
    assert len(read_lineage(p)) == 2


def test_lineage_survives_a_corrupt_line(tmp_path):
    p = tmp_path / "l.jsonl"
    p.write_text('{"good": 1}\nNOT JSON\n', encoding="utf-8")
    assert len(read_lineage(p)) == 1


def test_verify_lineage_detects_changed_data(tmp_path):
    data = tmp_path / "d.csv"
    data.write_text("a,b\n1,2\n", encoding="utf-8")
    row = record_lineage(tmp_path / "l.jsonl", data, "abc", {"p": 1}, "s")
    data.write_text("a,b\n9,9\n", encoding="utf-8")
    out = verify_lineage(row, data)
    assert out["ok"] is False
    assert any("data content changed" in p for p in out["problems"])


def test_verify_lineage_detects_changed_params(tmp_path):
    data = tmp_path / "d.csv"
    data.write_text("x\n", encoding="utf-8")
    row = record_lineage(tmp_path / "l.jsonl", data, "abc", {"p": 1}, "s")
    out = verify_lineage(row, data, {"p": 2})
    assert any("parameters differ" in p for p in out["problems"])


def test_verify_lineage_reports_a_dirty_tree(tmp_path):
    """A dirty tree is an honest warning, not a silent pass."""
    data = tmp_path / "d.csv"
    data.write_text("x\n", encoding="utf-8")
    row = record_lineage(tmp_path / "l.jsonl", data, "abc", {"p": 1}, "s")
    row["git_state"] = {"dirty": True, "commit": "abc", "available": True}
    out = verify_lineage(row, data, {"p": 1})
    assert any("DIRTY" in p for p in out["problems"])


def test_param_hash_ignores_key_order():
    assert sha256_params({"a": 1, "b": 2}) == sha256_params({"b": 2, "a": 1})


def test_code_state_never_raises():
    out = code_state()
    assert set(out) >= {"commit", "dirty", "available"}
