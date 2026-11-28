"""Credentials from the dashboard: four groups, saved into .env at once behind 2FA (2026-11-27).

Plain English: the Infrastructure box has four tabs (Broker, OpenAlgo, Screener, LLM/cloud API).
Each tab shows only the keys, passwords, usernames, TOTP secrets, URLs and models. Every other line in
.env is left alone. Typing a value and leaving the field saves it into the right .env file, after the
owner's 2FA code is checked.

Rules:
* Owner only (the caller checks). The owner must have a 2FA code enrolled, and the current code must
  match. No enrolled 2FA means no change at all.
* Only the names in ``FIELDS`` can be written. ``OPENALGO_API_KEY`` is display-only: the login flow
  keeps it in step with OpenAlgo, so it cannot be typed here.
* If the file exists, only that one line changes. If it does not exist, it is created with only this
  one key, at owner-only permissions (0600). A new file has no other secrets to lose.
* The preview shows the first 4 characters of a secret so the owner can tell it is the same value.
  Values of 4 characters or fewer show only asterisks. URLs and models show in full. No full secret
  value is ever returned, logged or audited. The audit records the NAME and the outcome only.

Flow stage: Dashboard -> credential upkeep (no order path)
Status: PHASE 4 IMPLEMENTED (2026-11-27; tests in tests/unit/test_credential_update.py)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from quant.core import dashboard_accounts as accounts
from quant.core.env_write import _quote, write_env_values, write_env_values_or_create
from quant.core.openalgo_apikey import read_env_value

REPO_ROOT = Path(__file__).resolve().parents[3]
RESTORED_ENV = REPO_ROOT / "_restored" / ".env"
REPO_ENV = REPO_ROOT / ".env"
MAX_VALUE = 256
PREVIEW_CHARS = 4

#: The four dashboard tabs. ``kind``: user | password | totp | key | url | model.
#: ``editable`` False = shown only (managed by another flow).
GROUPS: List[dict] = [
    {"id": "broker", "title": "Broker", "fields": [
        {"name": "BROKER_USERNAME", "kind": "user", "file": RESTORED_ENV, "editable": True},
        {"name": "BROKER_PASSWORD", "kind": "password", "file": RESTORED_ENV, "editable": True},
        {"name": "BROKER_TOTP_SECRET", "kind": "totp", "file": RESTORED_ENV, "editable": True},
        {"name": "BROKER_API_KEY", "kind": "key", "file": RESTORED_ENV, "editable": True},
        {"name": "BROKER_API_SECRET", "kind": "key", "file": RESTORED_ENV, "editable": True},
    ]},
    {"id": "openalgo", "title": "OpenAlgo", "fields": [
        {"name": "OPENALGO_URL", "kind": "url", "file": REPO_ENV, "editable": True},
        {"name": "OPENALGO_USERNAME", "kind": "user", "file": RESTORED_ENV, "editable": True},
        {"name": "OPENALGO_PASSWORD", "kind": "password", "file": RESTORED_ENV, "editable": True},
        {"name": "OPENALGO_TOTP_SECRET", "kind": "totp", "file": RESTORED_ENV, "editable": True},
        {"name": "OPENALGO_API_KEY", "kind": "key", "file": RESTORED_ENV, "editable": False},
    ]},
    {"id": "screener", "title": "Screener", "fields": [
        {"name": "SCREENER_USERNAME", "kind": "user", "file": REPO_ENV, "editable": True},
        {"name": "SCREENER_PASSWORD", "kind": "password", "file": REPO_ENV, "editable": True},
        {"name": "URL_SCREENER_DOT_IN", "kind": "url", "file": REPO_ENV, "editable": True},
    ]},
    {"id": "llm", "title": "LLM / cloud API", "fields": [
        {"name": "OLLAMA_HOST", "kind": "url", "file": REPO_ENV, "editable": True},
        {"name": "OLLAMA_MODEL", "kind": "model", "file": REPO_ENV, "editable": True},
        {"name": "ANTHROPIC_API_KEY", "kind": "key", "file": REPO_ENV, "editable": True},
        {"name": "OPENROUTER_API_KEY", "kind": "key", "file": REPO_ENV, "editable": True},
        {"name": "OPENROUTER_MODEL", "kind": "model", "file": REPO_ENV, "editable": True},
        {"name": "OPENAI_API_KEY", "kind": "key", "file": REPO_ENV, "editable": True},
    ]},
]

#: name -> file, for every name the dashboard may WRITE
ALLOWED: Dict[str, Path] = {f["name"]: f["file"] for g in GROUPS for f in g["fields"] if f["editable"]}
_FIELD_BY_NAME: Dict[str, dict] = {f["name"]: f for g in GROUPS for f in g["fields"]}


def owner_totp_ok(owner: str, code: str, users_path=accounts.DEFAULT_USERS) -> bool:
    """True only when the owner has a 2FA secret enrolled AND the code is correct now."""
    record = accounts._read(Path(users_path)).get(owner) or {}
    secret = str(record.get("totp_secret") or "")
    return bool(secret) and accounts.totp_verify(secret, str(code or ""))


def preview_of(value: str, kind: str) -> str:
    """What the dashboard may show for a value. Never the full secret."""
    if kind in ("url", "model"):
        return value
    if len(value) <= PREVIEW_CHARS:
        return "*" * len(value)
    return value[:PREVIEW_CHARS] + "…"


def status() -> List[dict]:
    """The four tabs: each field with whether it is set, its file and a safe preview. Never a full secret."""
    out = []
    for group in GROUPS:
        fields = []
        for f in group["fields"]:
            value = read_env_value(f["file"], f["name"])
            fields.append({
                "name": f["name"], "kind": f["kind"], "editable": f["editable"],
                "file": f["file"].name, "set": bool(value),
                "preview": preview_of(value, f["kind"]) if value else "",
            })
        out.append({"id": group["id"], "title": group["title"], "fields": fields})
    return out


def _create_with(target: Path, name: str, text: str) -> None:
    """Create a new .env holding only this one key, owner-only (0600). Never overwrites."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"{name}={_quote(text)}\n")


def update(name: str, value: str, *, owner: str, totp_code: str,
           users_path=accounts.DEFAULT_USERS, files: Optional[Dict[str, Path]] = None) -> Tuple[bool, str]:
    """(ok, reason). The reason is a fixed, safe sentence. Never raises for bad input."""
    targets = files if files is not None else ALLOWED
    if name not in targets:
        return False, "that credential cannot be changed from the dashboard"
    if not owner_totp_ok(owner, totp_code, users_path):
        return False, "2FA code is missing or wrong (2FA must be enrolled)"
    text = str(value or "")
    if not text.strip() or len(text) > MAX_VALUE or "\n" in text or "\r" in text or "\x00" in text:
        return False, "value is empty, too long or has a line break"
    target = Path(targets[name])
    try:
        if target.is_file():
            write_env_values(target, {name: text})
        else:
            _create_with(target, name, text)
        if files is None and target == RESTORED_ENV:
            # 2026-11-28 (item 2): the readers load the repo-root .env too, so keep it in step.
            write_env_values_or_create(REPO_ENV, {name: text})
    except FileExistsError:
        return False, f"{target.name} was just created by another save; try again"
    except Exception:  # noqa: BLE001 - the message must not carry the value
        return False, f"could not write {target.name}"
    return True, f"saved to {target.name}"
