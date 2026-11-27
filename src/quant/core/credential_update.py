"""Update one login credential from the dashboard, behind 2FA (owner decision 2026-11-27).

Rules:
* Owner only (the caller checks). The owner must have a 2FA code enrolled, and the current code must
  match. No enrolled 2FA means no change at all.
* Only the names in ``ALLOWED`` can be written. Each name has one fixed file. Broker and OpenAlgo
  names go to ``_restored/.env``. Screener.in names go to the repo ``.env``.
* A file is written only when it already exists. It is never created here.
* The value is never returned, logged or audited. The audit records the NAME and the outcome only.

Flow stage: Dashboard -> credential upkeep (no order path)
Status: PHASE 4 IMPLEMENTED (2026-11-27; tests in tests/unit/test_credential_update.py)
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

from quant.core import dashboard_accounts as accounts
from quant.core.env_write import write_env_values

REPO_ROOT = Path(__file__).resolve().parents[3]
RESTORED_ENV = REPO_ROOT / "_restored" / ".env"
REPO_ENV = REPO_ROOT / ".env"

#: name -> file that holds it
ALLOWED: Dict[str, Path] = {
    "OPENALGO_USERNAME": RESTORED_ENV,
    "OPENALGO_PASSWORD": RESTORED_ENV,
    "OPENALGO_TOTP_SECRET": RESTORED_ENV,
    "BROKER_USERNAME": RESTORED_ENV,
    "BROKER_PASSWORD": RESTORED_ENV,
    "BROKER_TOTP_SECRET": RESTORED_ENV,
    "SCREENER_USERNAME": REPO_ENV,
    "SCREENER_PASSWORD": REPO_ENV,
}
MAX_VALUE = 256


def owner_totp_ok(owner: str, code: str, users_path=accounts.DEFAULT_USERS) -> bool:
    """True only when the owner has a 2FA secret enrolled AND the code is correct now."""
    record = accounts._read(Path(users_path)).get(owner) or {}
    secret = str(record.get("totp_secret") or "")
    return bool(secret) and accounts.totp_verify(secret, str(code or ""))


def update(name: str, value: str, *, owner: str, totp_code: str,
           users_path=accounts.DEFAULT_USERS, files: Optional[Dict[str, Path]] = None) -> Tuple[bool, str]:
    """(ok, reason). The reason is a fixed, safe sentence. Never raises for bad input."""
    targets = files if files is not None else ALLOWED
    if name not in targets:
        return False, "that credential cannot be changed from the dashboard"
    if not owner_totp_ok(owner, totp_code, users_path):
        return False, "2FA code is missing or wrong (2FA must be enrolled)"
    text = str(value or "")
    if not text or len(text) > MAX_VALUE or "\n" in text or "\r" in text or "\x00" in text:
        return False, "value is empty, too long or has a line break"
    target = Path(targets[name])
    if not target.is_file():
        return False, f"{target.name} does not exist; it is never created here"
    write_env_values(target, {name: text})
    return True, "saved"
