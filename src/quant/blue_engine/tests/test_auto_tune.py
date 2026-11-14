"""Auto-tune tests (audit 2026-10-29).

Before this module, a triggered ReoptimizationSignal did exactly one thing:
latch the kill switch. The loop stopped trading but never re-optimized, so
nothing ever improved. These tests pin the safety properties of the conductor
that closes it:

* approval is RE-DERIVED from the report, never trusted from a boolean,
* missing out-of-sample statistics reject (fail closed),
* shadow mode never writes, apply mode validates before writing atomically,
* a cooldown prevents re-optimizing on every tick,
* nothing here un-halts anything.
"""
from __future__ import annotations

import json

import pytest

from blue_engine.core.auto_tune import AutoTuner, AutoTuneConfig, approval_ok


GOOD_ROW = {"symbol": "INFY", "approved": True, "is_sharpe": 1.8,
            "oos_sharpe": 1.5, "oos_dsr_probability": 0.98,
            "is_max_drawdown": -5.0, "oos_max_drawdown": -6.0,
            "best_parameters": {"min_body_pct": 0.35}}


def _report(rows, status="success", configurations=None):
    out = {"status": status, "results": rows}
    if configurations is not None:
        out["configurations"] = configurations
    return out


# --------------------------------------------------------------------------- #
# Approval policy
# --------------------------------------------------------------------------- #
def test_a_valid_result_is_approved():
    ok, why = approval_ok(_report([GOOD_ROW]), min_oos_sharpe=1.0,
                          oos_is_ratio=0.7, min_dsr_probability=0.95,
                          max_dd_ratio=1.3)
    assert ok is True
    assert "passed" in why


def test_missing_out_of_sample_statistics_reject():
    row = dict(GOOD_ROW)
    row.pop("oos_sharpe")
    ok, why = approval_ok(_report([row]), min_oos_sharpe=1.0, oos_is_ratio=0.7,
                          min_dsr_probability=0.95, max_dd_ratio=1.3)
    assert ok is False
    assert "missing out-of-sample" in why


def test_a_sharpe_below_the_floor_rejects():
    row = dict(GOOD_ROW, oos_sharpe=0.4)
    ok, why = approval_ok(_report([row]), min_oos_sharpe=1.0, oos_is_ratio=0.7,
                          min_dsr_probability=0.95, max_dd_ratio=1.3)
    assert ok is False
    assert "below the approval floor" in why


def test_oos_must_hold_up_against_is():
    # OOS 1.2 clears the absolute floor but is < 0.7 x IS 2.0.
    row = dict(GOOD_ROW, is_sharpe=2.0, oos_sharpe=1.2)
    ok, why = approval_ok(_report([row]), min_oos_sharpe=1.0, oos_is_ratio=0.7,
                          min_dsr_probability=0.95, max_dd_ratio=1.3)
    assert ok is False


def test_a_low_deflated_sharpe_rejects():
    row = dict(GOOD_ROW, oos_dsr_probability=0.5)
    ok, why = approval_ok(_report([row]), min_oos_sharpe=1.0, oos_is_ratio=0.7,
                          min_dsr_probability=0.95, max_dd_ratio=1.3)
    assert ok is False
    assert "deflated" in why.lower()


def test_excessive_out_of_sample_drawdown_rejects():
    row = dict(GOOD_ROW, is_max_drawdown=-5.0, oos_max_drawdown=-20.0)
    ok, why = approval_ok(_report([row]), min_oos_sharpe=1.0, oos_is_ratio=0.7,
                          min_dsr_probability=0.95, max_dd_ratio=1.3)
    assert ok is False
    assert "drawdown" in why.lower()


def test_an_empty_report_rejects():
    assert approval_ok(_report([]), min_oos_sharpe=1.0, oos_is_ratio=0.7,
                       min_dsr_probability=0.95, max_dd_ratio=1.3)[0] is False
    assert approval_ok({}, min_oos_sharpe=1.0, oos_is_ratio=0.7,
                       min_dsr_probability=0.95, max_dd_ratio=1.3)[0] is False


def test_a_failed_optimizer_status_rejects():
    ok, why = approval_ok(_report([GOOD_ROW], status="error"),
                          min_oos_sharpe=1.0, oos_is_ratio=0.7,
                          min_dsr_probability=0.95, max_dd_ratio=1.3)
    assert ok is False


def test_no_symbols_approved_rejects():
    row = dict(GOOD_ROW, approved=False)
    ok, why = approval_ok(_report([row]), min_oos_sharpe=1.0, oos_is_ratio=0.7,
                          min_dsr_probability=0.95, max_dd_ratio=1.3)
    assert ok is False
    assert "no symbol" in why


# --------------------------------------------------------------------------- #
# Controller behaviour
# --------------------------------------------------------------------------- #
def _tuner(tmp_path, *, mode="shadow", config_path=None, report=None,
           clock=None):
    cfg = AutoTuneConfig(mode=mode,
                         journal_path=str(tmp_path / "tune.jsonl"))
    return AutoTuner(cfg, batch_runner=lambda *a, **k: report or _report([GOOD_ROW]),
                     space_loader=lambda _s: {"x": 1},
                     clock=clock or (lambda: 0.0),
                     log=lambda _m: None)


def test_shadow_mode_computes_but_never_writes(tmp_path):
    live = tmp_path / "master.json"
    live.write_text(json.dumps({"symbol": "INFY", "exchange": "NSE",
                                "strategies": {"pov": True},
                                "pov": {"quantity": 10},
                                "ocl_keylevel": {"min_body_pct": 0.3}}),
                    encoding="utf-8")
    before = live.read_text(encoding="utf-8")
    tuner = _tuner(tmp_path, mode="shadow",
                  report=_report([GOOD_ROW],
                                 configurations={"INFY": {"min_body_pct": 0.35}}))
    result = tuner.on_signal(["drawdown breach"], symbol="INFY",
                             strategy="ocl_keylevel", config_path=str(live))
    assert result.approved is True
    assert result.promoted is False
    assert live.read_text(encoding="utf-8") == before, "shadow must not write"


def test_apply_mode_promotes_validated_parameters(tmp_path):
    live = tmp_path / "master.json"
    live.write_text(json.dumps({"symbol": "INFY", "exchange": "NSE",
                                "strategies": {"pov": True},
                                "pov": {"quantity": 10},
                                "ocl_keylevel": {"min_body_pct": 0.3}}),
                    encoding="utf-8")
    tuner = _tuner(tmp_path, mode="apply",
                  report=_report([GOOD_ROW],
                                 configurations={"INFY": {"min_body_pct": 0.35}}))
    result = tuner.on_signal(["drawdown breach"], symbol="INFY",
                             strategy="ocl_keylevel", config_path=str(live))
    assert result.promoted is True
    written = json.loads(live.read_text(encoding="utf-8"))
    assert written["ocl_keylevel"]["min_body_pct"] == 0.35
    # Everything else is preserved.
    assert written["pov"]["quantity"] == 10


def test_apply_mode_refuses_a_broken_candidate(tmp_path):
    live = tmp_path / "master.json"
    live.write_text(json.dumps({"symbol": "INFY", "exchange": "NSE",
                                "strategies": {"pov": True},
                                "pov": {"quantity": 10},
                                "ocl_keylevel": {"min_body_pct": 0.3}}),
                    encoding="utf-8")
    before = live.read_text(encoding="utf-8")
    tuner = _tuner(tmp_path, mode="apply",
                  report=_report([GOOD_ROW],
                                 configurations={"INFY": {"quantity": "nonsense"}}))
    result = tuner.on_signal(["x"], symbol="INFY", strategy="ocl_keylevel",
                             config_path=str(live))
    assert result.promoted is False
    assert live.read_text(encoding="utf-8") == before, "nothing may be written"


def test_cooldown_prevents_a_second_run(tmp_path):
    now = {"t": 100.0}
    tuner = _tuner(tmp_path, clock=lambda: now["t"])
    tuner.config.cooldown_seconds = 60.0
    first = tuner.on_signal(["a"], symbol="INFY", strategy="ocl_keylevel")
    assert first.attempted is True
    now["t"] = 130.0
    second = tuner.on_signal(["a"], symbol="INFY", strategy="ocl_keylevel")
    assert second.attempted is False
    assert "cooldown" in second.reason
    now["t"] = 200.0
    assert tuner.on_signal(["a"], symbol="INFY",
                           strategy="ocl_keylevel").attempted is True


def test_a_crashing_optimizer_never_raises(tmp_path):
    def boom(*_a, **_k):
        raise RuntimeError("optimizer exploded")

    tuner = AutoTuner(AutoTuneConfig(mode="shadow"), batch_runner=boom,
                      space_loader=lambda _s: {}, log=lambda _m: None)
    result = tuner.on_signal(["x"], symbol="INFY", strategy="ocl_keylevel")
    assert result.attempted is True
    assert result.approved is False
    assert result.errors


def test_every_attempt_is_journalled(tmp_path):
    tuner = _tuner(tmp_path)
    tuner.on_signal(["drawdown"], symbol="INFY", strategy="ocl_keylevel")
    journal = (tmp_path / "tune.jsonl").read_text(encoding="utf-8")
    assert "drawdown" in journal
    assert len(tuner.history) == 1


def test_invalid_mode_is_rejected_at_config_time():
    with pytest.raises(ValueError):
        AutoTuneConfig.from_dict({"mode": "unsafe"})


def test_auto_tune_does_not_touch_the_kill_switch(tmp_path, monkeypatch):
    """The tuner proposes parameters; only an operator re-arms trading."""
    import blue_engine.portfolio.optim_policy as policy

    called = {"reset": False, "trip": False}
    monkeypatch.setattr(policy, "reset_kill_switch",
                        lambda **k: called.update(reset=True))
    monkeypatch.setattr(policy, "trip_kill_switch",
                        lambda *a, **k: called.update(trip=True))
    _tuner(tmp_path).on_signal(["x"], symbol="INFY", strategy="ocl_keylevel")
    assert called == {"reset": False, "trip": False}
