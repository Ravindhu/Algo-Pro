"""Phase 1 exit-check gaps: override log, safe mode reason rule, ledger restart.

No openalgo or Blue_engine needed. Runs in any environment with pandas and pytest.
"""
import pytest

from quant.audit.override_log import record_override
from quant.monitor.ledger import Ledger
from quant.monitor.safe_mode import Mode, SafeMode


class FakeAudit:
    def __init__(self):
        self.rows = []

    def append(self, event, data):
        self.rows.append((event, data))
        return str(len(self.rows))


def test_override_needs_actor_and_reason():
    audit = FakeAudit()
    with pytest.raises(ValueError):
        record_override(audit, "", "reset kill switch", "maintenance")
    with pytest.raises(ValueError):
        record_override(audit, "owner", "reset kill switch", "")
    assert audit.rows == []


def test_override_written_with_actor_action_reason():
    audit = FakeAudit()
    record_override(audit, "owner", "reset kill switch", "after review")
    event, data = audit.rows[0]
    assert event == "override"
    assert data == {"actor": "owner", "action": "reset kill switch", "reason": "after review"}


def test_safe_mode_change_needs_reason_and_is_logged():
    audit = FakeAudit()
    sm = SafeMode(audit=audit)
    with pytest.raises(ValueError):
        sm.set(Mode.HALT_ALL, "")
    assert sm.mode == Mode.NORMAL and audit.rows == []
    sm.set(Mode.REDUCE_ONLY, "data feed stale")
    assert sm.mode == Mode.REDUCE_ONLY
    assert audit.rows[0][1]["reason"] == "data feed stale"


def test_ledger_remembers_intent_ids_after_restart(tmp_path):
    path = tmp_path / "ledger.jsonl"
    first = Ledger(path)
    first.append("intent", intent_id="INT-0001", symbol="RELIANCE")
    first.append("fill", intent_id="INT-0001", symbol="RELIANCE", signed_qty=10)

    # Simulate a restart: a new process rebuilds the ledger from the file.
    restarted = Ledger(path)
    assert "INT-0001" in restarted.seen_intent_ids()
    assert restarted.positions() == {"RELIANCE": 10}
