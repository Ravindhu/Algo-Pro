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

import base64
import struct

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_USERS = ROOT / "data" / "dashboard_users.json"
DEFAULT_AVATARS = ROOT / "data" / "dashboard_avatars"

MIN_PASSWORD = 12
ITERATIONS = 390_000
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
SESSION_TTL = 8 * 3600
RESET_TTL = 15 * 60          # forgot-password token lifetime
TOTP_PERIOD = 30             # RFC 6238 default
TOTP_DIGITS = 6
TOTP_DRIFT_STEPS = 1         # accept +/- one 30 s window
MAX_AVATAR_BYTES = 256 * 1024  # 256 KB cap; avatars are tiny by nature
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{1,31}$")

#: Account roles (plan: docs/IMPLEMENTATION_PLAN_ACCOUNTS_AND_RECOMMENDATIONS.md
#: Part A). Only "owner" may save recommendations. Accounts created before the
#: role field existed have no role: they read as "viewer", so the check fails
#: closed and privilege can only be granted deliberately.
OWNER, VIEWER = "owner", "viewer"
_ROLES = (OWNER, VIEWER)


def create_user(name: str, password: str, path: Path = DEFAULT_USERS, now: Optional[float] = None,
                role: str = VIEWER) -> None:
    """Add an account. Refuses a bad name, a short password, a bad role, or a duplicate."""
    if not _NAME_RE.match(name or ""):
        raise ValueError("name must start with a letter, 2-32 characters, letters/digits/._-")
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    if role not in _ROLES:
        raise ValueError(f"role must be one of {_ROLES}")
    users = _read(path)
    if name in users:
        raise ValueError(f"account {name!r} already exists")
    stamp = time.time() if now is None else now
    users[name] = {**hash_password(password), "created": int(stamp), "failures": 0,
                   "locked_until": 0, "role": role}
    _write(path, users)


# --------------------------------------------------------------------------- #
# Signup, password reset, profile picture, TOTP (user request 2026-11-12).
# Roles: self-signup ALWAYS creates a viewer - an owner exists only because the
# local CLI created one, so the web page can never raise privilege.
# --------------------------------------------------------------------------- #
def signup(name: str, password: str, path: Path = DEFAULT_USERS,
           now: Optional[float] = None) -> None:
    """Self-service account creation: always a VIEWER."""
    create_user(name, password, path, now=now, role=VIEWER)


def issue_reset_token(name: str, path: Path = DEFAULT_USERS,
                      now: Optional[float] = None) -> Optional[str]:
    """Create a one-use password-reset token. Returns the RAW token.

    The raw token is shown ONCE (server console/audit path), never stored in
    plain text - the file holds only its SHA-256. A token for an unknown name
    still spends the work but returns None (no account enumeration).
    """
    if name not in _read(path):
        return None
    stamp = time.time() if now is None else now
    raw = secrets.token_urlsafe(24)
    entry = {"hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
             "expires": stamp + RESET_TTL}
    users = _read(path)
    users[name]["reset"] = entry
    _write(path, users)
    return raw


def reset_password(name: str, token: str, new_password: str,
                   path: Path = DEFAULT_USERS, now: Optional[float] = None) -> Tuple[bool, str]:
    """Consume a reset token and set a new password. One use, time-limited."""
    stamp = time.time() if now is None else now
    users = _read(path)
    record = users.get(name)
    entry = (record or {}).get("reset") or {}
    if record is None or not entry:
        return False, "no reset was requested for this name"
    if entry.get("expires", 0) <= stamp:
        record.pop("reset", None)
        users[name] = record
        _write(path, users)
        return False, "reset token expired; request a new one"
    if not hmac.compare_digest(hashlib.sha256((token or "").encode("utf-8")).hexdigest(),
                               str(entry.get("hash", ""))):
        return False, "invalid reset token"
    if len(new_password or "") < MIN_PASSWORD:
        return False, f"password must be at least {MIN_PASSWORD} characters"
    record.pop("reset", None)
    record.update(hash_password(new_password))
    record["failures"] = 0
    record["locked_until"] = 0
    users[name] = record
    _write(path, users)
    return True, "ok"


def change_password(name: str, old_password: str, new_password: str,
                    path: Path = DEFAULT_USERS, now: Optional[float] = None) -> Tuple[bool, str]:
    """Signed-in password change: the OLD password is always required."""
    stamp = time.time() if now is None else now
    users = _read(path)
    record = users.get(name)
    if record is None or not _check(old_password or "", record):
        return False, "current password is wrong"
    if len(new_password or "") < MIN_PASSWORD:
        return False, f"password must be at least {MIN_PASSWORD} characters"
    record.update(hash_password(new_password))
    users[name] = record
    _write(path, users)
    return True, "ok"


def set_avatar(name: str, data: bytes, path: Path = DEFAULT_USERS,
               avatars_dir: Optional[Path] = None, now: Optional[float] = None) -> str:
    """Store a profile picture (PNG/JPEG bytes) under data/dashboard_avatars/.

    Size-capped and type-sniffed by magic bytes, never by filename. Returns
    the served path segment (/avatars/<name>.<ext>)."""
    avatars_dir = Path(avatars_dir) if avatars_dir else DEFAULT_AVATARS
    if name not in _read(path):
        raise ValueError("unknown account")
    if not data or len(data) > MAX_AVATAR_BYTES:
        raise ValueError(f"picture must be 1 byte to {MAX_AVATAR_BYTES // 1024} KB")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        ext = "jpg"
    else:
        raise ValueError("only PNG or JPEG pictures are accepted")
    avatars_dir.mkdir(parents=True, exist_ok=True)
    target = avatars_dir / f"{name}.{ext}"
    target.write_bytes(data)
    # Drop the other extension so exactly one avatar exists per account.
    for stale in avatars_dir.glob(f"{name}.*"):
        if stale != target:
            stale.unlink()
    users = _read(path)
    users[name]["avatar"] = f"/avatars/{name}.{ext}"
    users[name]["avatar_updated"] = int(time.time() if now is None else now)
    _write(path, users)
    return users[name]["avatar"]


def avatar_file(name: str, avatars_dir: Optional[Path] = None) -> Optional[Path]:
    """The avatar file for an account, or None. Path built from a whitelisted
    name (the regex forbids slashes), so traversal is impossible."""
    if not _NAME_RE.match(name or ""):
        return None
    avatars_dir = Path(avatars_dir) if avatars_dir else DEFAULT_AVATARS
    for candidate in sorted(avatars_dir.glob(f"{name}.*")):
        if candidate.suffix.lower() in (".png", ".jpg", ".jpeg"):
            return candidate
    return None


# --------------------------------------------------------------------------- #
# TOTP (RFC 6238), pure stdlib: hmac/sha1, base32 secret, 30 s step, 6 digits.
# Enrollment: generate_totp_secret() once; provision shows the key + an
# otpauth:// URI for Google Authenticator; totp_verify() gates login when a
# secret is enrolled. The secret is stored in the users file, which is runtime
# state - it must never be committed.
# --------------------------------------------------------------------------- #
def generate_totp_secret() -> str:
    """160-bit random secret, base32-encoded (Google Authenticator compatible)."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _totp_code(secret_b32: str, counter: int) -> str:
    pad = "=" * ((8 - len(secret_b32.replace("=", "")) % 8) % 8)
    key = base64.b32decode((secret_b32.replace("=", "") + pad).upper())
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF)
    return str(value % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def totp_now(secret_b32: str, now: Optional[float] = None) -> str:
    """The current expected code (tests and provisioning display only)."""
    stamp = time.time() if now is None else now
    return _totp_code(secret_b32, int(stamp // TOTP_PERIOD))


def totp_verify(secret_b32: str, code: str, now: Optional[float] = None) -> bool:
    """Constant-shape check of a 6-digit code, +/- one time window."""
    if not secret_b32 or not re.fullmatch(r"\d{%d}" % TOTP_DIGITS, str(code or "")):
        return False
    stamp = time.time() if now is None else now
    counter = int(stamp // TOTP_PERIOD)
    # Same work for every candidate window: no timing leak about which matched.
    for step in (-TOTP_DRIFT_STEPS, 0, TOTP_DRIFT_STEPS):
        if hmac.compare_digest(_totp_code(secret_b32, counter + step), code):
            return True
    return False


def totp_enroll(name: str, secret_b32: str, path: Path = DEFAULT_USERS) -> str:
    """Store a pending secret; it becomes REQUIRED at the next login.

    Returns the otpauth:// URI for Google Authenticator (issuer Algo-Pro).
    The URI is built ONLY from the name (regex-validated) and the secret.
    """
    users = _read(path)
    record = users.get(name)
    if record is None:
        raise ValueError("unknown account")
    if not re.fullmatch(r"[A-Z2-7]{32}", (secret_b32 or "").upper()):
        raise ValueError("secret must be base32 (32 chars A-Z2-7)")
    normalized = secret_b32.upper().replace("=", "")
    record["totp_secret"] = normalized
    users[name] = record
    _write(path, users)
    return (f"otpauth://totp/Algo-Pro:{name}?secret={normalized}"
            f"&issuer=Algo-Pro&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_PERIOD}")


def totp_pending(name: str, path: Path = DEFAULT_USERS) -> bool:
    """True when the account requires a TOTP code at login."""
    record = _read(path).get(name)
    return bool(record and record.get("totp_secret"))


def totp_provision(name: str, path: Path = DEFAULT_USERS) -> Tuple[str, str]:
    """(secret, otpauth_uri) for the account's enrolled secret.

    Shown once in the account section so the key can be typed into Google
    Authenticator. Requires the OWNER (the route enforces it); this helper is
    the pure read.
    """
    record = _read(path).get(name)
    secret = (record or {}).get("totp_secret") or ""
    if not secret:
        raise ValueError("no TOTP secret enrolled for this account")
    uri = (f"otpauth://totp/Algo-Pro:{name}?secret={secret}"
           f"&issuer=Algo-Pro&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_PERIOD}")
    return secret, uri


def totp_remove(name: str, path: Path = DEFAULT_USERS) -> bool:
    """Un-enroll TOTP. Owner-only at the route; here just the file change."""
    users = _read(path)
    record = users.get(name)
    if record is None or "totp_secret" not in record:
        return False
    del record["totp_secret"]
    users[name] = record
    _write(path, users)
    return True


def account_role(name: str, path: Path = DEFAULT_USERS) -> str:
    """The account's role, read fresh from the file on EVERY call.

    Reading per request (never caching) is what makes a demotion bite on the
    next request, with no re-login. Unknown account or unreadable file ->
    "viewer", so access fails closed.
    """
    record = _read(path).get(name)
    if not isinstance(record, dict):
        return VIEWER
    role = record.get("role", VIEWER)
    return role if role in _ROLES else VIEWER


def is_owner(name: Optional[str], path: Path = DEFAULT_USERS) -> bool:
    """True only for a signed-in account whose stored role is "owner"."""
    return bool(name) and account_role(name, path) == OWNER


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


def verify_login(name: str, password: str, path: Path = DEFAULT_USERS, now: Optional[float] = None,
                 totp_code: str = "") -> Tuple[bool, str]:
    """(ok, reason). The reason never says whether the name exists.

    When the account has a TOTP secret enrolled, a correct password alone is
    NOT enough: the current 6-digit code from the authenticator is required
    too ("totp_required" is returned so the UI can show the code field).
    """
    stamp = time.time() if now is None else now
    users = _read(path)
    record = users.get(name)
    if record is None:
        # Spend the same work as a real check, so timing does not reveal names.
        hash_password(password or "x")
        return False, "invalid name or password"
    if record.get("locked_until", 0) > stamp:
        return False, "account is locked after too many failures; try again later"
    secret = str(record.get("totp_secret") or "")
    if secret and not totp_verify(secret, totp_code, now=stamp):
        return False, "totp_required"
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
