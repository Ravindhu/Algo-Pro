"""Named dashboard accounts and signed session cookies.

Status: PHASE 3 IMPLEMENTED (accounts and signed cookies; tests in tests/unit/test_dashboard_accounts.py)
Plan: docs/SCHEDULER_PLAN.md (Accounts section).

* Passwords are never stored. Only a PBKDF2-SHA256 hash with a per-user salt.
* After 5 wrong passwords an account is locked for 15 minutes.
* A session cookie is "name|expiry" signed with HMAC-SHA256 using
  DASHBOARD_SESSION_SECRET. With no secret, account login is refused (fail closed).
* The users file is runtime state (data/dashboard_users.json, git-ignored).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_USERS = ROOT / "data" / "dashboard_users.json"

MIN_PASSWORD = 12
ITERATIONS = 390_000
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
SESSION_TTL = 8 * 3600
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,31}$")


def hash_password(password: str, salt: Optional[bytes] = None, iterations: int = ITERATIONS) -> Dict[str, object]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {"salt": salt.hex(), "hash": digest.hex(), "iterations": iterations}


def _check(password: str, record: Dict[str, object]) -> bool:
    try:
        salt = bytes.fromhex(str(record["salt"]))
        expected = bytes.fromhex(str(record["hash"]))
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(record["iterations"]))
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def _read(path: Path) -> Dict[str, dict]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        users = data.get("users") if isinstance(data, dict) else None
        return users if isinstance(users, dict) else {}
    except (OSError, ValueError):
        # Unreadable file: no accounts, so login fails closed.
        return {}


def _write(path: Path, users: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".users-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"users": users}, stream, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def create_user(name: str, password: str, path: Path = DEFAULT_USERS, now: Optional[float] = None) -> None:
    """Add an account. Refuses a bad name, a short password, or a duplicate."""
    if not _NAME_RE.match(name or ""):
        raise ValueError("name must start with a letter, 2-32 characters, letters/digits/._-")
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    users = _read(path)
    if name in users:
        raise ValueError(f"account {name!r} already exists")
    stamp = time.time() if now is None else now
    users[name] = {**hash_password(password), "created": int(stamp), "failures": 0, "locked_until": 0}
    _write(path, users)


def verify_login(name: str, password: str, path: Path = DEFAULT_USERS, now: Optional[float] = None) -> Tuple[bool, str]:
    """(ok, reason). The reason never says whether the name exists."""
    stamp = time.time() if now is None else now
    users = _read(path)
    record = users.get(name)
    if record is None:
        # Spend the same work as a real check, so timing does not reveal names.
        hash_password(password or "x")
        return False, "invalid name or password"
    if record.get("locked_until", 0) > stamp:
        return False, "account is locked after too many failures; try again later"
    if _check(password or "", record):
        record["failures"] = 0
        record["locked_until"] = 0
        users[name] = record
        _write(path, users)
        return True, "ok"
    record["failures"] = int(record.get("failures", 0)) + 1
    if record["failures"] >= MAX_FAILURES:
        record["locked_until"] = int(stamp) + LOCK_SECONDS
        record["failures"] = 0
    users[name] = record
    _write(path, users)
    return False, "invalid name or password"


def make_session(name: str, secret: str, now: Optional[float] = None) -> str:
    expiry = int((time.time() if now is None else now) + SESSION_TTL)
    payload = f"{name}|{expiry}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def read_session(value: str, secret: str, now: Optional[float] = None) -> Optional[str]:
    """The account name if the cookie is genuine and not expired, else None."""
    if not secret or not value or "." not in value:
        return None
    payload, _, sig = value.rpartition(".")
    expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    name, _, expiry = payload.partition("|")
    try:
        if int(expiry) < int(time.time() if now is None else now):
            return None
    except ValueError:
        return None
    return name or None
