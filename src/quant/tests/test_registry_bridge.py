"""Approved-model handoff: status, signature and human sign-off must ALL agree.

AGENTS.md rule 5 says only approved models enter production and that a person
signs off. These tests pin the three independent refusals, because any one of
them missing is enough for an unproven model to reach live capital.
"""
import pytest

from quant.models import registry_bridge as rb


def _manifest(**over):
    base = {"status": rb.STATUS_VALIDATED, "signature": "sig"}
    base.update(over)
    return base


def test_describe_reports_availability():
    """The panel must render a reason, not crash, whatever the registry says."""
    out = rb.describe()
    assert "available" in out
    assert "detail" in out
    assert isinstance(out["statuses"], dict)


def test_no_approver_is_refused():
    with pytest.raises(rb.ApprovalRequired, match="approver"):
        rb.load_approved(_manifest(), "addr")


def test_blank_approver_is_refused():
    with pytest.raises(rb.ApprovalRequired, match="approver"):
        rb.load_approved(_manifest(), "addr", approver="   ", reason="because")


def test_no_reason_is_refused():
    with pytest.raises(rb.ApprovalRequired, match="reason"):
        rb.load_approved(_manifest(), "addr", approver="alice")


def test_draft_status_is_refused():
    """Only 'validated' may be loaded in production."""
    with pytest.raises(rb.ApprovalRequired, match="only 'validated'"):
        rb.load_approved(_manifest(status=rb.STATUS_DRAFT), "addr",
                         approver="alice", reason="trying it on")


def test_retired_status_is_refused():
    with pytest.raises(rb.ApprovalRequired):
        rb.load_approved(_manifest(status=rb.STATUS_RETIRED), "addr",
                         approver="alice", reason="old model")


def test_missing_manifest_or_address_is_refused():
    with pytest.raises(rb.ApprovalRequired):
        rb.load_approved({}, "addr", approver="alice", reason="r")
    with pytest.raises(rb.ApprovalRequired):
        rb.load_approved(_manifest(), "", approver="alice", reason="r")


def test_unverified_signature_is_refused():
    """A validated manifest with a bad signature must never load."""
    with pytest.raises(rb.ApprovalRequired, match="signature not verified"):
        rb.load_approved(_manifest(signature="forged"), "addr",
                         approver="alice", reason="r")


def test_status_constants_match_the_engine():
    assert (rb.STATUS_DRAFT, rb.STATUS_VALIDATED, rb.STATUS_RETIRED) == (
        "draft", "validated", "retired")


def test_approval_required_is_a_permission_error():
    """Callers catching PermissionError must not miss this refusal."""
    assert issubclass(rb.ApprovalRequired, PermissionError)
