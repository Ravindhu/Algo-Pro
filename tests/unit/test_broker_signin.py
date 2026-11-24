"""Broker login after a dashboard sign-in: runs live-enabled, audits only the result, never raises."""
import json

from quant.core import broker_signin as bs


def test_login_runs_with_live_allowed_and_audits_success(tmp_path):
    audit = tmp_path / "audit.jsonl"
    calls = []

    def fake_runner(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "leg": "broker", "reason": "attempt completed", "skipped": False}

    result = bs.login_after_signin("Owner", audit, runner=fake_runner)
    assert result["ok"] is True
    assert calls == [{"enable": True, "allow_live": True}]
    row = json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])["payload"]
    assert row["ok"] is True and row["actor"] == "Owner"


def test_login_exception_is_contained_and_audited(tmp_path):
    audit = tmp_path / "audit.jsonl"

    def boom(**kwargs):
        raise RuntimeError("secret-looking text must not be audited")

    result = bs.login_after_signin("Owner", audit, runner=boom)
    assert result == {"ok": False, "reason": "RuntimeError", "leg": ""}
    text = audit.read_text(encoding="utf-8")
    assert "secret-looking" not in text
    assert '"ok": false' in text


def test_skipped_cooldown_is_audited_as_skipped(tmp_path):
    audit = tmp_path / "audit.jsonl"

    def cooldown(**kwargs):
        return {"ok": False, "skipped": True, "cooldown": True, "reason": "wait 90 s", "leg": ""}

    bs.login_after_signin("Owner", audit, runner=cooldown)
    row = json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])["payload"]
    assert row["skipped"] is True and row["reason"] == "wait 90 s"
