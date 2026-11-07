"""Reconciliation job (flow item 11). Fake broker reader; ledger is a real file in tmp_path."""
from quant.audit.log import AuditLog
from quant.monitor.ledger import Ledger
from quant.monitor.recon_job import run_recon
from quant.notify.alerts import Alerts


def _ledger(tmp_path, fills):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    for symbol, qty in fills:
        ledger.append("fill", symbol=symbol, signed_qty=qty)
    return ledger


def test_match_is_logged_as_info(tmp_path):
    ledger = _ledger(tmp_path, [("INFY", 10)])
    audit, alerts = AuditLog(tmp_path / "audit.jsonl"), Alerts()
    result = run_recon(ledger, lambda: {"INFY": 10}, audit=audit, alerts=alerts)
    assert result == {"status": "match", "mismatches": {}}
    assert alerts.items[-1]["level"] == "INFO"
    assert audit.read_all()[-1]["kind"] == "recon"


def test_mismatch_is_error_with_both_quantities(tmp_path):
    ledger = _ledger(tmp_path, [("INFY", 10)])
    alerts = Alerts()
    result = run_recon(ledger, lambda: {"INFY": 7}, alerts=alerts)
    assert result["status"] == "mismatch"
    assert result["mismatches"] == {"INFY": [10, 7]}
    assert alerts.items[-1]["level"] == "ERROR"


def test_unreadable_broker_is_warning_and_changes_nothing(tmp_path):
    ledger = _ledger(tmp_path, [("INFY", 10)])
    before = ledger.positions()

    def broken():
        raise TimeoutError("broker timeout")

    alerts = Alerts()
    result = run_recon(ledger, broken, alerts=alerts)
    assert result["status"] == "unavailable"
    assert alerts.items[-1]["level"] == "WARNING"
    assert ledger.positions() == before
