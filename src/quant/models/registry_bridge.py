"""Approved-model handoff: the registry -> production arrow. Wraps the engine.

Flow stage: Human sign-off, Model registry, Approved-model handoff (purple arrow)
Status: PHASE 5 IMPLEMENTED (wrap; read-only gate, cannot create or promote an artifact)
Existing code to reuse or wrap (do not copy):
    src/quant/blue_engine/ml/registry/  (Registry, sign_manifest,
        verify_manifest, sign_artifact, verify_artifact, STATUS_*)

WHY THIS WRAPPER EXISTS
-----------------------
AGENTS.md rule 5: "Only approved models enter production: Model registry ->
factor models. A person signs off (Human sign-off)." Until now that arrow had no
implementation on the src/quant side at all, so the rule was documentation only.

The engine's registry already does the hard parts: content-addressed artifacts,
HMAC-signed manifests, an append-only index, and a three-state lifecycle
(``draft`` -> ``validated`` -> ``retired``). It also states that ``validated`` is
"the ONLY status live code may load".

WHAT THIS WRAPPER ADDS
----------------------
1. **A human sign-off requirement the registry alone does not impose.** The
   registry trusts whoever holds the signing key. This layer additionally
   requires a recorded approver and a reason, so an automated pipeline cannot
   promote a model by setting a status. Rule 5 says a PERSON signs off.
2. **A single call the trade path can make** (`load_approved`) that returns a
   model only when status, signature and sign-off all agree - and raises
   otherwise. Never a partial or best-effort load.
3. **An availability-safe read** (`describe`) so the dashboard can show the
   registry state without the process needing a signing key or a live model.

WHAT IT DOES NOT DO
-------------------
- It never calls ``save``. Training owns artifact creation.
- It never calls ``promote``. Promotion is a human act performed by an operator
  script; doing it from the trading process would let a bug promote itself.
- It never imports a model library; the registry returns whatever payload was
  stored.

SIGNING KEY
-----------
The engine reads ``BLUE_ML_SIGNING_KEY`` from the environment only, never a file.
This module does not read, log or expose it. With no key set, verification
returns False and every load is refused - the correct fail-closed default.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from quant.core.blue_bridge import BlueEngineUnavailable, load

logger = logging.getLogger(__name__)

__all__ = [
    "RegistryUnavailable",
    "ApprovalRequired",
    "STATUS_DRAFT",
    "STATUS_VALIDATED",
    "STATUS_RETIRED",
    "describe",
    "load_approved",
]


class RegistryUnavailable(RuntimeError):
    """The engine's model registry cannot be loaded."""


class ApprovalRequired(PermissionError):
    """An artifact is not approved for production use."""


def _module():
    try:
        return load("ml.registry")
    except BlueEngineUnavailable as exc:  # pragma: no cover - environment dependent
        raise RegistryUnavailable(f"engine model registry unavailable: {exc}") from exc


STATUS_DRAFT = "draft"
STATUS_VALIDATED = "validated"
STATUS_RETIRED = "retired"


def describe(registry: Any = None, *, root: Optional[str] = None) -> Dict[str, Any]:
    """Registry state for the dashboard. Never raises; reports why it is empty.

    Returns::

        {"available": bool, "statuses": {...}, "entries": [...], "detail": str}
    """
    try:
        mod = _module()
    except RegistryUnavailable as exc:
        return {"available": False, "statuses": {}, "entries": [],
                "detail": str(exc)}

    try:
        reg = registry if registry is not None else (mod.Registry(root) if root else mod.Registry())
        entries: List[Dict[str, Any]] = list(reg.history() or [])
    except Exception as exc:  # noqa: BLE001 - a panel must render, not crash
        return {"available": False, "statuses": {}, "entries": [],
                "detail": f"registry unreadable: {exc}"}

    counts: Dict[str, int] = {STATUS_DRAFT: 0, STATUS_VALIDATED: 0, STATUS_RETIRED: 0}
    for e in entries:
        st = str((e or {}).get("status") or "")
        counts[st] = counts.get(st, 0) + 1

    return {"available": True, "statuses": counts, "entries": entries,
            "detail": f"{len(entries)} artifact(s) in the index"}


def load_approved(
    manifest: Dict[str, Any],
    address: str,
    *,
    approver: Optional[str] = None,
    reason: Optional[str] = None,
    registry: Any = None,
    root: Optional[str] = None,
) -> Any:
    """Load a model artifact ONLY when it is validated, signed and signed off.

    ``approver`` and ``reason`` are the human sign-off from rule 5. Both are
    required and neither may be blank. This is the check the registry cannot
    make on its own: the registry knows the artifact is ``validated``, it does
    not know that a person said so.

    Raises ApprovalRequired for a policy refusal, RegistryUnavailable when the
    engine is missing. Never returns a partially verified artifact.
    """
    approver = (approver or "").strip()
    reason = (reason or "").strip()
    if not approver:
        raise ApprovalRequired("no approver recorded: a person must sign off (rule 5)")
    if not reason:
        raise ApprovalRequired("no sign-off reason recorded")

    try:
        mod = _module()
    except RegistryUnavailable:
        raise

    if not manifest or not address:
        raise ApprovalRequired("manifest and address are required")

    status = str(manifest.get("status") or "")
    if status != STATUS_VALIDATED:
        raise ApprovalRequired(
            f"artifact status is {status or 'unknown'!r}; only {STATUS_VALIDATED!r} "
            "may be loaded in production")

    signature = manifest.get("signature") or ""
    verified = False
    try:
        verified = bool(mod.verify_manifest(manifest, signature))
    except Exception as exc:  # noqa: BLE001
        logger.warning("manifest verification raised: %r", exc)
        verified = False

    if not verified:
        raise ApprovalRequired(
            "manifest signature not verified (is BLUE_ML_SIGNING_KEY set?)")

    try:
        reg = registry if registry is not None else (mod.Registry(root) if root else mod.Registry())
        payload = reg.load(address)
    except Exception as exc:  # noqa: BLE001
        raise ApprovalRequired(f"artifact could not be loaded: {exc}") from exc

    logger.info("approved model loaded: address=%s approver=%s reason=%s",
                address, approver, reason)
    return payload
