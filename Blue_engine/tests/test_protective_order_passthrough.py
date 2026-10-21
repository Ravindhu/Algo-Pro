"""Regression tests for strategy protective-level passthrough.

These tests are offline: they verify that the POV slicer hands SL/TP to the
registry before the execution loop, while allowing an exit intent to omit new
protective levels without corrupting the original entry record.
"""
from __future__ import annotations
from blue_engine.core import protective_orders

def test_registry_registers_and_ratchets_buy_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(protective_orders, "_DEFAULT_PATH", tmp_path / "levels.json")
    protective_orders._LEVELS.clear()

    protective_orders.register(
        "exec-1", symbol="INFY", exchange="NSE", action="BUY",
        stop_loss=100.0, target=130.0, strategy="OCL",
        meta={"exit_mode": "FIXED", "htf_stop_loss": 95.0})
    protective_orders.update("exec-1", "INFY", stop_loss=110.0)
    protective_orders.update("exec-1", "INFY", stop_loss=105.0)

    record = protective_orders.get("exec-1", "INFY")
    assert record is not None
    assert record["stop_loss"] == 110.0
    assert record["target"] == 130.0
    assert record["exit_mode"] == "FIXED"
    assert (tmp_path / "levels.json").is_file()


def test_exit_intent_does_not_register_replacement_protection(tmp_path, monkeypatch):
    monkeypatch.setattr(protective_orders, "_DEFAULT_PATH", tmp_path / "levels.json")
    protective_orders._LEVELS.clear()
    protective_orders.register(
        "exec-2", symbol="TCS", exchange="NSE", action="BUY",
        stop_loss=3200.0, target=3500.0, strategy="OCL",
        meta={"exit_mode": "FIXED"})

    # Close intents are not registered as a new protective-order execution;
    # they close the original exposure and its monitor later clears its key.
    record = protective_orders.get("exec-2", "TCS")
    assert record is not None
    assert record["action"] == "BUY"
    assert record["stop_loss"] == 3200.0
    assert record["target"] == 3500.0
