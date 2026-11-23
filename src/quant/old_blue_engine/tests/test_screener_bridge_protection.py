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
    # These tests exercise protective-order plumbing, not the live money-cap
    # policy (covered in test_risk_gate and test_safety_fixes). Opt out of the
    # live acknowledgement explicitly so the fixtures stay focused.
    cfg = dict(risk_config or {})
    cfg.setdefault("require_live_ack", False)
    return ScreenerBridge(
        symbols=["INFY"], risk_config=cfg,
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
    bridge._open_positions["INFY|NSE"] = 7
    result = bridge.execute_intent("INFY", "NSE", {
        "action": "SELL", "quantity": 10,
        "_meta": {"position_exit": True, "exit_reason": "target"}})
    assert result["filled"] == 7
    _, intent = StubSlicer.calls[0]
    assert intent["action"] == "SELL"
    assert intent["quantity"] == 7
    assert bridge._open_positions.get("INFY|NSE", 0) == 0


# --------------------------------------------------------------------------- #
# Per-symbol exchange (mixed NSE / BSE screens need no override)
# --------------------------------------------------------------------------- #
def test_normalize_symbols_keeps_pairs_and_defaults():
    from blue_engine.core.screener_bridge import normalize_symbols
    out = normalize_symbols(
        ["infy", ("500319", "bse"), ("TCS", None), "INFY", ("infy", "NSE")],
        "NSE")
    assert out == [("INFY", "NSE"), ("500319", "BSE"), ("TCS", "NSE")]


def test_bridge_builds_signal_with_the_row_exchange(monkeypatch):
    import blue_engine.core.screener_bridge as module
    monkeypatch.setattr(module, "init_client", lambda **kwargs: StubClient())
    bridge = module.ScreenerBridge(
        symbols=[("INFY", "NSE"), ("500319", "BSE")], exchange="NSE")
    nse_signal = bridge._build_signal("INFY", "NSE")
    bse_signal = bridge._build_signal("500319", "BSE")
    assert nse_signal.exchange == "NSE"
    assert bse_signal.exchange == "BSE"


def test_exit_tracking_is_keyed_per_symbol_and_exchange(monkeypatch):
    bridge = _bridge(monkeypatch)
    bridge.execute_intent("INFY", "NSE", {
        "action": "BUY", "quantity": 5, "price": 100.0, "_meta": {}})
    assert bridge._open_positions == {"INFY|NSE": 5}
    # An exit on the OTHER exchange must not touch this position.
    assert bridge.execute_intent("INFY", "BSE", {
        "action": "SELL", "quantity": 5,
        "_meta": {"position_exit": True}}) is None
    assert bridge._open_positions == {"INFY|NSE": 5}


# --------------------------------------------------------------------------- #
# Risk wiring (audit 2026-10-29): the bridge must feed the gate the same
# live context the runner does, and must fail closed when it cannot.
# --------------------------------------------------------------------------- #
def _bridge_no_price(monkeypatch, risk_config):
    bridge = _bridge(monkeypatch, risk_config)
    monkeypatch.setattr(ScreenerBridge, "_arrival_price",
                        lambda self, symbol, exchange: None)
    return bridge


def test_entry_without_a_quote_is_blocked_when_money_caps_are_set(monkeypatch):
    # max_order_notional cannot be evaluated without a price -> fail closed.
    bridge = _bridge_no_price(monkeypatch, {"max_order_notional": 250000.0,
                                    "max_daily_loss": 10000.0,
                                    "max_open_exposure": 500000.0})
    result = bridge.execute_intent("INFY", "NSE", {
        "action": "BUY", "quantity": 10, "stop_loss": 95.0, "target": 115.0,
        "_meta": {}})
    assert result is None
    assert StubSlicer.calls == []
    assert bridge._executed[-1]["status"] == "risk_gate_block"


def test_quote_is_used_when_the_intent_has_no_price(monkeypatch):
    bridge = _bridge(monkeypatch, {"max_order_quantity": 20})
    monkeypatch.setattr(ScreenerBridge, "_arrival_price",
                        lambda self, symbol, exchange: 100.0)
    result = bridge.execute_intent("INFY", "NSE", {
        "action": "BUY", "quantity": 10, "_meta": {}})
    assert result["filled"] == 10


def test_daily_loss_comes_from_the_broker_not_from_config(monkeypatch):
    bridge = _bridge(monkeypatch, {
        "max_daily_loss": 1000.0, "max_order_quantity": 20})
    monkeypatch.setattr(
        ScreenerBridge, "_risk_view",
        lambda self: {"config": self.risk_config,
                      "context": {"day_pnl": -5000.0, "open_exposure": 0.0}})
    result = bridge.execute_intent("INFY", "NSE", {
        "action": "BUY", "quantity": 10, "price": 100.0, "_meta": {}})
    assert result is None, "a breached daily loss must block the entry"
    assert bridge._executed[-1]["status"] == "risk_gate_block"
    assert StubSlicer.calls == []


def test_an_exit_is_allowed_while_the_daily_loss_is_breached(monkeypatch):
    bridge = _bridge(monkeypatch, {
        "max_daily_loss": 1000.0, "max_order_quantity": 20})
    bridge._open_positions["INFY|NSE"] = 10
    monkeypatch.setattr(
        ScreenerBridge, "_risk_view",
        lambda self: {"config": self.risk_config,
                      "context": {"day_pnl": -5000.0, "open_exposure": 0.0}})
    result = bridge.execute_intent("INFY", "NSE", {
        "action": "SELL", "quantity": 10, "price": 100.0,
        "_meta": {"position_exit": True}})
    assert result is not None, "risk must never trap an open position"


def test_unreadable_mode_is_treated_as_live(monkeypatch):
    import blue_engine.core.mode as mode_module
    bridge = _bridge(monkeypatch)

    def _boom(_client):
        raise RuntimeError("no such method")
    monkeypatch.setattr(mode_module, "read_mode", _boom)
    assert bridge._live_mode() is True
    monkeypatch.setattr(mode_module, "read_mode", lambda _c: "analyze")
    assert bridge._live_mode() is False
