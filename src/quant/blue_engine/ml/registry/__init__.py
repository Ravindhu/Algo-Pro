"""
ml/registry — the artifact vault. The ONLY way a trained model reaches live use.

THE ONE JOB
-----------
A trained model is worthless (dangerous, actually) until three things are true:

    1. it is stored together with its MANIFEST (what data, what features, what
       params, what seed, what splits — everything needed to rebuild it),
    2. the manifest is SIGNED and verified on load (an edited manifest is a
       different artifact — silent mutation is how a bad model gets trusted),
    3. its status is ``validated`` (walk-forward passed, cost gate passed).
       Live code refuses anything else: `ModelNotValidatedError`.

Files (all under ``data/ml_artifacts/``):
    <sha256>/model.json      the serialised model payload
    <sha256>/manifest.json   provenance + evaluation + HMAC signature
    registry.jsonl           append-only index: one line per artifact event

Why a JSONL index: it survives a crash mid-append (each line flushes), diffs
cleanly in git, and the dashboard can tail it without a DB.

SECURITY MODEL
--------------
The signature (v2) is an HMAC-SHA256 over a canonical envelope of the
manifest, the payload address (sha256 of ``model.json``) and the status,
keyed from ``BLUE_ML_SIGNING_KEY`` (env). Load also re-hashes ``model.json``
and checks it matches its folder address, so a swapped model, a relocated
manifest or a relabelled status is refused. Legacy v1 artifacts (manifest-only
signature) still load, with a warning; ``promote()`` re-signs them as v2.
Without the env var the registry refuses to
SIGN (nothing ships unsigned) but will still VERIFY pre-signed artifacts —
so a read-only dashboard box needs no secret at all. The key is never
written to disk, never logged, and never sent anywhere: credentials-safe by
construction (AGENTS.md).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, List, Optional

from .. import config, logger
from ..errors import ModelNotValidatedError

__all__ = [
    "Registry", "sign_manifest", "verify_manifest", "canonical_json",
    "STATUS_DRAFT", "STATUS_VALIDATED", "STATUS_RETIRED",
]

STATUS_DRAFT = "draft"            # trained, evaluation not (yet) passed
STATUS_VALIDATED = "validated"    # the ONLY status live code may load
STATUS_RETIRED = "retired"        # superseded or withdrawn — never load


def canonical_json(obj: Any) -> str:
    """Stable JSON text: sorted keys, no whitespace, floats via repr.

    Hashing a dict WITHOUT canonicalisation is a classic trap: two runs of the
    same logical content hash differently (key order, float formatting), and
    the manifest then fails verification for no reason. This is the single
    place that defines "same content".
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=repr)


def content_hash(obj: Any) -> str:
    """sha256 hex of :func:`canonical_json`. Used for artifact addressing."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def _signing_key() -> Optional[bytes]:
    """Read the signing secret from the environment ONLY (never from disk)."""
    secret = os.environ.get("BLUE_ML_SIGNING_KEY", "")
    return secret.encode("utf-8") if secret else None


def sign_manifest(manifest: Dict[str, Any]) -> str:
    """HMAC-SHA256 over the canonical manifest. Raises if no key is set.

    Refusing to sign without a key is deliberate: an unsigned artifact cannot
    be loaded live (verify fails), so a misconfigured environment fails at
    TRAINING time — where the operator is watching — instead of at TRADING
    time, where it would not be.
    """
    key = _signing_key()
    if not key:
        raise RuntimeError(
            "BLUE_ML_SIGNING_KEY is not set: refusing to sign an artifact. "
            "Set it (env only, never a file) and re-run.")
    return hmac.new(key, canonical_json(manifest).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def verify_manifest(manifest: Dict[str, Any], signature: str) -> bool:
    """Constant-time signature check. No key -> not verified (read-only mode).

    LEGACY (sig_version 1): covers the manifest only. Kept so artifacts saved
    before the v2 envelope still load; ``promote()`` re-signs them as v2.
    """
    key = _signing_key()
    if not key:
        return False
    return hmac.compare_digest(
        hmac.new(key, canonical_json(manifest).encode("utf-8"),
                 hashlib.sha256).hexdigest(),
        signature)


SIG_VERSION = 2


def _artifact_envelope(manifest: Dict[str, Any], address: str,
                       status: str) -> Dict[str, Any]:
    """Everything that makes an artifact THIS artifact, in one signed object.

    The v1 signature covered only the manifest, so a model.json swapped on
    disk, a folder renamed, or an unsigned ``status`` field could all be
    changed without breaking it. v2 binds the payload address and the status.
    """
    return {"manifest": manifest, "payload_sha256": address, "status": status}


def sign_artifact(manifest: Dict[str, Any], address: str, status: str) -> str:
    """HMAC-SHA256 over the v2 envelope. Raises if no key is set."""
    key = _signing_key()
    if not key:
        raise RuntimeError(
            "BLUE_ML_SIGNING_KEY is not set: refusing to sign an artifact. "
            "Set it (env only, never a file) and re-run.")
    return hmac.new(key, canonical_json(
        _artifact_envelope(manifest, address, status)).encode("utf-8"),
        hashlib.sha256).hexdigest()


def verify_artifact(manifest: Dict[str, Any], address: str, status: str,
                    signature: str) -> bool:
    """Constant-time check of the v2 envelope. No key -> not verified."""
    key = _signing_key()
    if not key:
        return False
    expected = hmac.new(key, canonical_json(
        _artifact_envelope(manifest, address, status)).encode("utf-8"),
        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class Registry:
    """Store/load/verify artifacts + the append-only index."""

    def __init__(self, root: Optional[str] = None):
        self.root = root or config.ARTIFACT_DIR
        os.makedirs(self.root, exist_ok=True)
        self.index_path = os.path.join(self.root, "registry.jsonl")

    # ---- write ------------------------------------------------------------ #

    def save(self, payload: Dict[str, Any], manifest: Dict[str, Any],
             ) -> Dict[str, Any]:
        """Persist a model + signed manifest. Returns the record written.

        The artifact's folder name is the sha256 of the PAYLOAD — content
        addressing means an identical model can never be stored twice with two
        different truths, and a mutated payload cannot keep its address.
        """
        address = content_hash(payload)
        art_dir = os.path.join(self.root, address)
        os.makedirs(art_dir, exist_ok=True)

        status = manifest.get("status", STATUS_DRAFT)
        signature = sign_artifact(manifest, address, status)
        record = {
            "address": address,
            "name": manifest.get("name", "model"),
            "status": status,
            "sig_version": SIG_VERSION,
            "created_at": int(time.time()),
            "signature": signature,
            "manifest": manifest,
        }
        with open(os.path.join(art_dir, "model.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, default=repr)
        with open(os.path.join(art_dir, "manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({**record, "manifest_signature": signature}, fh,
                      sort_keys=True, indent=1, default=repr)
        self._append_index(record)
        logger.info("registry: saved artifact %s (%s, status=%s)",
                    address[:12], record["name"], record["status"])
        return record

    def _append_index(self, record: Dict[str, Any]) -> None:
        """One line per event. Best-effort: an index write must never crash a
        run that already succeeded — but a failure IS logged loudly."""
        try:
            with open(self.index_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True, default=repr) + "\n")
        except OSError as exc:
            logger.error("registry: index append failed: %r", exc)

    # ---- read ------------------------------------------------------------- #

    def load(self, address: str, *,
             require_status: Optional[str] = STATUS_VALIDATED) -> Dict[str, Any]:
        """Load + verify one artifact. Refuses anything unverified/unvalidated.

        Raises:
            ModelNotValidatedError: signature mismatch, missing manifest, or
                status below ``require_status``. Live code catches this and
                shows a BLOCKED badge — it NEVER falls back to a stale model.
        """
        art_dir = os.path.join(self.root, address)
        try:
            with open(os.path.join(art_dir, "manifest.json"), "r",
                      encoding="utf-8") as fh:
                rec = json.load(fh)
            with open(os.path.join(art_dir, "model.json"), "r",
                      encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelNotValidatedError(
                f"artifact {address[:12]} unreadable: {exc!r}") from exc

        sig = rec.get("manifest_signature", "")
        manifest = rec.get("manifest", {})
        status = rec.get("status", STATUS_DRAFT)
        if rec.get("sig_version", 1) >= SIG_VERSION:
            signed_ok = verify_artifact(manifest, address, status, sig)
        else:
            signed_ok = verify_manifest(manifest, sig)
        if not signed_ok:
            raise ModelNotValidatedError(
                f"artifact {address[:12]}: signature FAILED — "
                f"do not trust this artifact; retrain or re-sign consciously")
        # The folder name is the payload hash. Check the bytes on disk match
        # it, and that the record claims this same address. Both checks hold
        # for legacy artifacts too, so a swapped model.json is always caught.
        if rec.get("address") != address:
            raise ModelNotValidatedError(
                f"artifact {address[:12]}: record names a different address "
                f"- refusing to load")
        if content_hash(payload) != address:
            raise ModelNotValidatedError(
                f"artifact {address[:12]}: model.json does not match its "
                f"address (payload altered after signing) - refusing to load")
        if rec.get("sig_version", 1) < SIG_VERSION:
            logger.warning("registry: artifact %s uses legacy signature v1; "
                           "promote() re-signs it with v2", address[:12])

        if require_status and status != require_status:
            raise ModelNotValidatedError(
                f"artifact {address[:12]} status is '{status}', "
                f"live code requires '{require_status}'")
        return {"payload": payload, "manifest": rec["manifest"],
                "status": status, "address": address}

    def history(self, name: Optional[str] = None, *,
                limit: int = 50) -> List[Dict[str, Any]]:
        """Read the index (newest last). Optional filter by model name."""
        if not os.path.isfile(self.index_path):
            return []
        out: List[Dict[str, Any]] = []
        with open(self.index_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue          # a torn last line from a killed process
                if name and rec.get("name") != name:
                    continue
                out.append(rec)
        return out[-limit:]

    def promote(self, address: str, *, to: str = STATUS_VALIDATED) -> bool:
        """Move an artifact's status forward (draft -> validated, or retire).

        Re-signs with the CURRENT manifest (a promotion is a decision, and the
        signature must reflect the decision, not the original training run).
        """
        art_dir = os.path.join(self.root, address)
        mpath = os.path.join(art_dir, "manifest.json")
        try:
            with open(mpath, "r", encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("registry: promote failed for %s: %r", address, exc)
            return False
        rec["status"] = to
        rec["promoted_at"] = int(time.time())
        rec["sig_version"] = SIG_VERSION
        rec["manifest_signature"] = sign_artifact(
            rec["manifest"], rec.get("address", address), to)
        try:
            with open(mpath, "w", encoding="utf-8") as fh:
                json.dump(rec, fh, sort_keys=True, indent=1, default=repr)
        except OSError as exc:
            logger.error("registry: promote write failed: %r", exc)
            return False
        self._append_index({k: rec[k] for k in
                            ("address", "name", "status", "signature")})
        logger.info("registry: %s -> %s", address[:12], to)
        return True
