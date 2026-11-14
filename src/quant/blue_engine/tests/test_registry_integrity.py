"""Model registry: a loaded artifact must be exactly the one that was signed.

The manifest HMAC covers the manifest only. Before this test existed, a
``model.json`` swapped on disk (same folder, same signed manifest) loaded
without any complaint, so a tampered model could go live.
"""
from __future__ import annotations

import json
import os

import pytest

from blue_engine.ml.errors import ModelNotValidatedError
from blue_engine.ml.registry import (
    STATUS_VALIDATED,
    Registry,
    content_hash,
)

_KEY = "unit-test-signing-key"


@pytest.fixture
def signed_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUE_ML_SIGNING_KEY", _KEY)
    reg = Registry(root=str(tmp_path / "artifacts"))
    payload = {"weights": [0.1, 0.2, 0.3], "bias": 0.05}
    manifest = {"name": "alpha_mlp", "status": STATUS_VALIDATED,
                "evaluation": {"sharpe": 1.1}}
    record = reg.save(payload, manifest)
    return reg, record["address"]


def _art(reg, address, fname):
    return os.path.join(reg.root, address, fname)


def test_clean_artifact_loads(signed_registry):
    reg, address = signed_registry
    out = reg.load(address)
    assert out["payload"]["bias"] == 0.05


def test_swapped_model_payload_is_refused(signed_registry):
    reg, address = signed_registry
    # Attacker/bit-rot replaces the weights; manifest and signature untouched.
    with open(_art(reg, address, "model.json"), "w", encoding="utf-8") as fh:
        json.dump({"weights": [9.9, 9.9, 9.9], "bias": 9.9}, fh)
    with pytest.raises(ModelNotValidatedError, match="payload"):
        reg.load(address)


def test_record_relabelled_to_another_address_is_refused(signed_registry):
    reg, address = signed_registry
    rec_path = _art(reg, address, "manifest.json")
    with open(rec_path, encoding="utf-8") as fh:
        rec = json.load(fh)
    rec["address"] = "0" * 64                    # claims a different identity
    with open(rec_path, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    with pytest.raises(ModelNotValidatedError):
        reg.load(address)


def test_address_matches_payload_hash(signed_registry):
    reg, address = signed_registry
    with open(_art(reg, address, "model.json"), encoding="utf-8") as fh:
        payload = json.load(fh)
    assert content_hash(payload) == address


def test_relocated_signed_manifest_is_refused(signed_registry, tmp_path):
    """A validly signed manifest copied under another payload's address."""
    reg, address = signed_registry
    other_payload = {"weights": [1.0], "bias": 0.0}
    other_addr = content_hash(other_payload)
    os.makedirs(os.path.join(reg.root, other_addr), exist_ok=True)
    with open(os.path.join(reg.root, other_addr, "model.json"), "w",
              encoding="utf-8") as fh:
        json.dump(other_payload, fh)
    with open(_art(reg, address, "manifest.json"), encoding="utf-8") as fh:
        stolen = fh.read()
    with open(os.path.join(reg.root, other_addr, "manifest.json"), "w",
              encoding="utf-8") as fh:
        fh.write(stolen)                  # signature was made for `address`
    with pytest.raises(ModelNotValidatedError):
        reg.load(other_addr)


def test_status_cannot_be_relabelled_without_resigning(signed_registry):
    """Changing the record's status (either way) must break the signature."""
    reg, address = signed_registry
    rec_path = _art(reg, address, "manifest.json")
    with open(rec_path, encoding="utf-8") as fh:
        original = json.load(fh)
    for forged in ("retired", "draft"):
        rec = json.loads(json.dumps(original))
        rec["status"] = forged
        with open(rec_path, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        with pytest.raises(ModelNotValidatedError):
            reg.load(address, require_status=None)


def test_promote_resigns_and_reloads(signed_registry):
    reg, address = signed_registry
    assert reg.promote(address, to="validated") is True
    assert reg.load(address)["status"] == "validated"
