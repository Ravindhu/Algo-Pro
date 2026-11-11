"""Live compliance gates: SEBI algo tagging, audit-log storage, NTP clock sync."""
from quant.risk.compliance import ComplianceConfig, ComplianceGate, check_audit_storage


def _gate(tmp_path, **over):
    cfg = dict(algo_id="ALGO-123", sebi_registration="INH000012345",
               audit_dir=str(tmp_path), min_free_bytes=0)
    cfg.update(over)
    return ComplianceGate(ComplianceConfig(**cfg), clock_check=lambda: {"ok": True, "skew_seconds": 0.1})


def test_all_checks_pass(tmp_path):
    assert _gate(tmp_path).check().ok


def test_missing_algo_id_blocks(tmp_path):
    r = _gate(tmp_path, algo_id=None).check()
    assert not r.ok and any("algo ID" in x for x in r.reasons)


def test_missing_sebi_registration_blocks(tmp_path):
    r = _gate(tmp_path, sebi_registration=None).check()
    assert not r.ok and any("SEBI registration" in x for x in r.reasons)


def test_intent_algo_id_can_supply_tag(tmp_path):
    g = _gate(tmp_path, algo_id=None)
    assert g.algo_tagging_ok("EXCH-ALGO-9").ok


def test_audit_dir_missing_blocks(tmp_path):
    r = check_audit_storage(tmp_path / "nope")
    assert not r.ok and any("missing" in x for x in r.reasons)


def test_audit_dir_low_space_blocks(tmp_path):
    r = check_audit_storage(tmp_path, min_free_bytes=10 ** 18)
    assert not r.ok and any("low" in x for x in r.reasons)


def test_clock_not_configured_fails_closed(tmp_path):
    g = ComplianceGate(ComplianceConfig(algo_id="A", sebi_registration="S", audit_dir=str(tmp_path)))
    r = g.clock_ok()
    assert not r.ok and any("NTP" in x for x in r.reasons)


def test_clock_skewed_blocks(tmp_path):
    g = ComplianceGate(ComplianceConfig(algo_id="A", sebi_registration="S", audit_dir=str(tmp_path)),
                       clock_check=lambda: {"ok": False, "skew_seconds": 5.0})
    r = g.clock_ok()
    assert not r.ok and any("skew" in x for x in r.reasons)


def test_clock_check_raising_fails_closed(tmp_path):
    def boom():
        raise RuntimeError("ntp down")

    g = ComplianceGate(ComplianceConfig(algo_id="A", sebi_registration="S", audit_dir=str(tmp_path)),
                       clock_check=boom)
    assert not g.clock_ok().ok
