"""Offline smoke test for screener bridge protective execution handoff."""
from __future__ import annotations
from blue_engine.core import protective_orders
from blue_engine.core.screener_bridge import ScreenerBridge
from blue_engine.core.risk_gate import RiskGate
class StubClient:
    pass
class StubSlicer:
    calls = []
    def __init__(self, client, config):
        self.config = config
    def _register_protection(self, intent):
        """Mirror the real slicer's registry handoff (POV does this
        inside execute(); the stub must too, or the handoff is untested)."""
        meta = intent.get("_meta") or {}
        protective_orders.register(
            "stub-exec", symbol=self.config["symbol"],
            exchange=self.config.get("exchange", "NSE"),
            action=intent["action"],
            stop_loss=intent.get("stop_loss"),
            target=intent.get("target"),
            strategy=self.config.get("strategy_label", ""),
            meta=meta, persist=False)
    def execute(self, intent):
        self.calls.append((self.config, intent))
        self._register_protection(intent)
        return {"filled": intent["quantity"], "child_orders": 1}


def _bridge(monkeypatch, risk_config=None):
    import blue_engine.core.screener_bridge as module
    monkeypatch.setattr(module, "init_client", lambda **kwargs: StubClient())
    import blue_engine.pov.slicers.pov as pov_module
    monkeypatch.setattr(pov_module, "POVSlicer", StubSlicer)
    monkeypatch.setattr(ScreenerBridge, "kill_tripped", lambda self: False)
    StubSlicer.calls.clear()
    protective_orders._LEVELS.clear()
    monkeypatch.setattr(protective_orders, "_persist", lambda: None)
    return ScreenerBridge(
        symbols=["INFY"], risk_config=risk_config,
        pov_params={"duration": 1, "interval": 1})


def test_risk_gate_blocks_before_slicer(monkeypatch):
    bridge = _bridge(monkeypatch, {"max_order_quantity": 5})
    result = bridge.execute_intent("INFY", "NSE", {
        "action": "BUY", "quantity": 10, "price": 100.0,
        "stop_loss": 95.0, "target": 115.0, "_meta": {}})
    assert result is None
    assert StubSlicer.calls == []


def test_entry_passes_protection_to_pov_and_registry(monkeypatch):
    bridge = _bridge(monkeypatch, {"max_order_quantity": 20})
    result = bridge.execute_intent("INFY", "NSE", {
        "action": "BUY", "quantity": 10, "price": 100.0,
        "stop_loss": 95.0, "htf_stop_loss": 90.0, "target": 115.0,
        "_meta": {"exit_mode": "FIXED", "htf_stop_loss": 90.0}})
    assert result["filled"] == 10
    cfg, intent = StubSlicer.calls[0]
    assert cfg["symbol"] == "INFY"
    assert cfg["stop_loss_price"] == 95.0
    assert cfg["target_price"] == 115.0
    assert intent["stop_loss"] == 95.0
    record = protective_orders.snapshot()[0]
    assert record["stop_loss"] == 95.0
    assert record["target"] == 115.0
    assert record["htf_stop_loss"] == 90.0

def test_close_intent_uses_sell_and_tracked_quantity(monkeypatch):
    bridge = _bridge(monkeypatch)
    bridge._open_positions["INFY"] = 7
    result = bridge.execute_intent("INFY", "NSE", {
        "action": "SELL", "quantity": 10,
        "_meta": {"position_exit": True, "exit_reason": "target"}})
    assert result["filled"] == 7
    _, intent = StubSlicer.calls[0]
    assert intent["action"] == "SELL"
    assert intent["quantity"] == 7
    assert bridge._open_positions.get("INFY", 0) == 0
