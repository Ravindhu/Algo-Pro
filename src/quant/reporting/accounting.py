"""Authorized accounting view and FIFO tax lots (STCG vs LTCG by holding period). Rates are not set here.

Flow stage: Dashboard (accounting view), Tax lots
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): Blue_engine/jobs/daily_portfolio.py
"""
from __future__ import annotations

import hashlib
import hmac
import os
from collections import deque
from datetime import date
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

# scrypt cost: ~16 MB memory per hash. Fixed here so stored hashes stay verifiable.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _DKLEN = 2 ** 14, 8, 1, 32
MIN_PASSWORD_LEN = 12
PASSWORD_FILE_ENV = "ACCOUNTING_PASSWORD_FILE"
# Default lives in logs/ (git-ignored). The hash is never committed.
DEFAULT_PASSWORD_FILE = Path(__file__).resolve().parents[3] / "logs" / "accounting_password.hash"

LTCG_DAYS = 365  # holding period threshold for listed equity. Confirm current rule with a tax adviser.


def require_role(role: str, allowed: Tuple[str, ...] = ("accountant", "owner")) -> None:
    if role not in allowed:
        raise PermissionError("accounting view needs an authorised role")


def fifo_realised(buys: List[Tuple[date, float, float]], sells: List[Tuple[date, float, float]]) -> List[Dict]:
    """buys/sells: (date, qty, price). Sells match the oldest open lots first."""
    lots: Deque[list] = deque([d, q, p] for d, q, p in buys)
    out = []
    for sd, sq, sp in sells:
        remaining = sq
        while remaining > 0 and lots:
            bd, bq, bp = lots[0]
            take = min(bq, remaining)
            out.append({"buy_date": bd, "sell_date": sd, "qty": take,
                        "pnl": take * (sp - bp),
                        "term": "LTCG" if (sd - bd).days > LTCG_DAYS else "STCG"})
            remaining -= take
            lots[0][1] -= take
            if lots[0][1] == 0:
                lots.popleft()
        if remaining > 0:
            raise ValueError("sell exceeds open lots")
    return out


# --- local password for the accounting view -----------------------------------

def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    """Return 'scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>'. Never store the plain password."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    salt = salt if salt is not None else os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                            r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check against a stored hash. Any malformed input returns False."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(str(password).encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(hash_hex)))
        return hmac.compare_digest(digest, bytes.fromhex(hash_hex))
    except (ValueError, TypeError, AttributeError):
        return False


def password_file(path: Optional[Path] = None) -> Path:
    """Path to the stored hash: explicit arg, then env var, then the git-ignored default."""
    if path is not None:
        return Path(path)
    env = os.environ.get(PASSWORD_FILE_ENV)
    return Path(env) if env else DEFAULT_PASSWORD_FILE


def set_password(password: str, path: Optional[Path] = None) -> Path:
    """Write the hash to the password file. Overwrites any existing hash."""
    target = password_file(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(hash_password(password), encoding="utf-8")
    return target


def authorise_accounting(role: str, password: str, path: Optional[Path] = None) -> None:
    """Role check first, then the local password. Raises PermissionError on any failure.

    The password is never logged or echoed in the error message.
    """
    require_role(role)
    target = password_file(path)
    if not target.exists():
        raise PermissionError("accounting password not set")
    if not verify_password(password, target.read_text(encoding="utf-8").strip()):
        raise PermissionError("accounting password rejected")
