"""Auto-tune must never move risk bounds (audit 2026-11-03, fixes 4 and 7).

A refused promotion writes nothing. A failed atomic write leaves no temp file.
"""
from __future__ import annotations

import json
import sys
import types

from blue_engine.core import auto_tune as at


def test_risk_bounds_are_recognised():
    for key in ("max_order_quantity", "max_order_notional", "max_daily_loss",
                "max_open_exposure", "blocked", "require_live_ack",
                "circuit_fail_closed", "kill_switch_on", "dry_run",
                "stop_loss_pct"):
        assert at.is_risk_key(key), key


def test_ordinary_strategy_parameters_stay_tunable():
    for key in ("ema_fast", "ema_slow", "lookback", "entry_threshold"):
        assert not at.is_risk_key(key), key


def test_promotion_touching_a_risk_key_is_refused_and_writes_nothing(tmp_path):
    cfg = tmp_path / "live.json"
    original = {"ocl_keylevel": {"lookback": 20, "max_order_quantity": 500}}
    cfg.write_text(json.dumps(original))
    tuner = at.AutoTuner(at.AutoTuneConfig(mode="apply"))
    ok, why = tuner._promote(str(cfg), "ocl_keylevel",
                             {"lookback": 30, "max_order_quantity": 5000})
    assert ok is False
    assert "max_order_quantity" in why
    # The whole promotion is refused, so the ordinary key is not written either.
    assert json.loads(cfg.read_text()) == original


def test_failed_atomic_write_leaves_no_temp_file(tmp_path, monkeypatch):
    # Stub the parser so the test reaches the write step.
    stub = types.ModuleType("blue_engine.core.config_loader")
    stub.parse_master_config = lambda _cfg: None
    monkeypatch.setitem(sys.modules, "blue_engine.core.config_loader", stub)
    cfg = tmp_path / "live.json"
    cfg.write_text(json.dumps({"ocl": {"lookback": 20}}))

    def _fail_replace(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(at.os, "replace", _fail_replace)
    tuner = at.AutoTuner(at.AutoTuneConfig(mode="apply"))
    ok, why = tuner._promote(str(cfg), "ocl", {"lookback": 30})
    assert ok is False
    assert "atomic write failed" in why
    assert not list(tmp_path.glob("*.tmp"))
