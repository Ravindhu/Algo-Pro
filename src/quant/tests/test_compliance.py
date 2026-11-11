"""Live compliance gates: audit-log storage and NTP clock sync (owner removed the SEBI
algo-ID / registration checks on 2026-11-11 — registration is the broker's obligation)."""
from quant.risk.compliance import ComplianceConfig, ComplianceGate, check_audit_storage


def _gate(tmp_path, **over):
    cfg = dict(audit_dir=str(tmp_path), min_free_bytes=0)
    cfg.update(over)
    return ComplianceGate(ComplianceConfig(**cfg), clock_check=lambda: {"ok": True, "skew_seconds": 0.1})


def test_all_checks_pass(tmp_path):
    assert _gate(tmp_path).check().ok


def test_no_sebi_fields_in_config(tmp_path):
    """The SEBI algo-ID / registration fields are gone from the config (owner 2026-11-11)."""
    assert not hasattr(ComplianceConfig(), "algo_id")
    assert not hasattr(ComplianceConfig(), "sebi_registration")


def test_check_takes_no_algo_argument(tmp_path):
    import inspect

    assert "algo" not in inspect.signature(ComplianceGate.check).parameters


def test_audit_dir_missing_blocks(tmp_path):
    r = check_audit_storage(tmp_path / "nope")
    assert not r.ok and any("missing" in x for x in r.reasons)


def test_audit_dir_low_space_blocks(tmp_path):
    r = check_audit_storage(tmp_path, min_free_bytes=10 ** 18)
    assert not r.ok and any("low" in x for x in r.reasons)


def test_clock_not_configured_fails_closed(tmp_path):
    g = ComplianceGate(ComplianceConfig(audit_dir=str(tmp_path)))
    r = g.clock_ok()
    assert not r.ok and any("NTP" in x for x in r.reasons)


def test_clock_skewed_blocks(tmp_path):
    g = ComplianceGate(ComplianceConfig(audit_dir=str(tmp_path)),
                       clock_check=lambda: {"ok": False, "skew_seconds": 5.0})
    r = g.clock_ok()
    assert not r.ok and any("skew" in x for x in r.reasons)


def test_clock_check_raising_fails_closed(tmp_path):
    def boom():
        raise RuntimeError("ntp down")

    g = ComplianceGate(ComplianceConfig(audit_dir=str(tmp_path)), clock_check=boom)
    assert not g.clock_ok().ok
