"""Nightly backup and restore (flow item 18). Real files in tmp_path."""
from datetime import datetime, timezone

import pytest

from quant.audit.log import AuditLog
from quant.monitor.backup import backup, restore
from quant.monitor.ledger import Ledger


def _seed(tmp_path):
    ledger_path = tmp_path / "live" / "ledger.jsonl"
    audit_path = tmp_path / "live" / "audit.jsonl"
    limits_path = tmp_path / "live" / "risk_limits.yaml"
    ledger = Ledger(ledger_path)
    ledger.append("fill", symbol="INFY", signed_qty=10)
    audit = AuditLog(audit_path)
    audit.append("kill_reset", {"reason": "test"})
    audit.append("recon", {"status": "match"})
    limits_path.write_text("max_daily_loss: 1000\n", encoding="utf-8")
    return {"ledger": ledger_path, "audit": audit_path, "limits": limits_path}


def test_restore_round_trip_keeps_ledger_and_audit_valid(tmp_path):
    files = _seed(tmp_path)
    when = datetime(2026, 11, 7, 21, 0, tzinfo=timezone.utc)
    folder = backup(files, tmp_path / "backups", now=when)
    assert folder.name == "20261107T210000Z"

    targets = {k: tmp_path / "restored" / p.name for k, p in files.items()}
    restore(folder, targets)

    assert Ledger(targets["ledger"]).positions() == {"INFY": 10}
    assert AuditLog(targets["audit"]).verify() is True
    assert targets["limits"].read_text(encoding="utf-8") == "max_daily_loss: 1000\n"


def test_restore_refuses_to_overwrite_without_force(tmp_path):
    files = _seed(tmp_path)
    folder = backup(files, tmp_path / "backups")
    with pytest.raises(FileExistsError):
        restore(folder, files)  # targets are the live files, which exist


def test_tampered_backup_is_refused(tmp_path):
    files = _seed(tmp_path)
    folder = backup(files, tmp_path / "backups")
    # backup() stores each file under its key ("ledger"), so tamper with that stored file
    (folder / "ledger").write_text('{"event":"fill","symbol":"X","signed_qty":999}\n', encoding="utf-8")
    targets = {"ledger": tmp_path / "restored" / "ledger.jsonl"}
    with pytest.raises(ValueError, match="checksum mismatch"):
        restore(folder, targets)
    assert not targets["ledger"].exists()


def test_missing_file_is_recorded_as_none(tmp_path):
    files = {"ledger": tmp_path / "nothing_here.jsonl"}
    folder = backup(files, tmp_path / "backups")
    assert (folder / "manifest.json").exists()
    assert '"ledger": null' in (folder / "manifest.json").read_text(encoding="utf-8")


def test_existing_backup_folder_is_never_overwritten(tmp_path):
    files = _seed(tmp_path)
    when = datetime(2026, 11, 7, 21, 0, tzinfo=timezone.utc)
    backup(files, tmp_path / "backups", now=when)
    with pytest.raises(FileExistsError):
        backup(files, tmp_path / "backups", now=when)
