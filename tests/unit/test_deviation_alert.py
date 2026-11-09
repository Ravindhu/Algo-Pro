"""Strategy-deviation alert (flow item 22): CUSUM alarm connected to alerts."""
from quant.monitor.strategy_health import emit_deviation_alert
from quant.notify.alerts import Alerts


def test_alarm_emits_error():
    alerts = Alerts()
    fired = emit_deviation_alert(alerts, "strat-A", [-0.05] * 30, mu0=0.0, k=0.0, h=0.5)
    assert fired is True
    assert alerts.items[-1]["level"] == "ERROR"
    assert alerts.items[-1]["component"] == "strat-A"


def test_no_drift_emits_nothing():
    alerts = Alerts()
    fired = emit_deviation_alert(alerts, "strat-A", [0.01] * 30, mu0=0.0, k=0.0, h=0.5)
    assert fired is False
    assert alerts.items == []
