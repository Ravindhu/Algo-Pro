"""Automatic rotation of the dashboard login (session) key.

Status: PHASE 3 IMPLEMENTED (tests in tests/unit/test_key_rotation.py). Plan:
docs/IMPLEMENTATION_PLAN_2026-11-25_KEY_ROTATION.md

* The key file (data/session_secret.key) keeps its plain text format. Its
  modification time is the key's creation time, so no new format is needed.
* A key older than the maximum age is replaced with a new random key. The
  write is atomic and the file stays owner-only (0600).
* Rotation is OFF while DASHBOARD_SESSION_SECRET is set in the environment:
  an env value is the owner's explicit pin and this code never overrides it.
* Rotating logs every signed-in browser out once. That is the purpose.
* No key value is returned or logged. Callers get a short reason only.
"""
from __future__ import annotations

import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple

DEFAULT_MAX_AGE_DAYS = 30
ENV_MAX_AGE = "DASHBOARD_KEY_MAX_AGE_DAYS"
KEY_BYTES = 48  # same strength as the first-start key


def max_age_seconds(raw: Optional[str] = None) -> float:
    """Maximum key age in seconds from DASHBOARD_KEY_MAX_AGE_DAYS.

    Missing or unreadable values fall back to the default. 0 turns rotation off.
    Negative values are treated as the default (never as "rotate always").
    """
    text = os.environ.get(ENV_MAX_AGE, "") if raw is None else raw
    try:
        days = float(text) if str(text).strip() else float(DEFAULT_MAX_AGE_DAYS)
    except ValueError:
        days = float(DEFAULT_MAX_AGE_DAYS)
    if days < 0:
        days = float(DEFAULT_MAX_AGE_DAYS)
    return days * 86400.0


def key_age_seconds(path: Path, now: Optional[float] = None) -> Optional[float]:
    """Seconds since the key file was written, or None when there is no key file."""
    if not path.is_file():
        return None
    stamp = time.time() if now is None else now
    return max(0.0, stamp - path.stat().st_mtime)


def _write_private(path: Path, text: str) -> None:
    """Atomic owner-only write: a temp file in the same folder, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + "-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def rotate_if_due(path: Path, max_age_s: float, now: Optional[float] = None,
                  env_pinned: bool = False) -> Tuple[bool, str]:
    """Replace the key when it is due. Returns (rotated, reason). Never raises.

    On any write error the old key stays in place and the reason says why.
    """
    if env_pinned:
        return False, "pinned by DASHBOARD_SESSION_SECRET in the environment"
    if max_age_s <= 0:
        return False, "rotation is off (DASHBOARD_KEY_MAX_AGE_DAYS = 0)"
    age = key_age_seconds(path, now)
    if age is None:
        return False, "no key file yet"
    if age < max_age_s:
        return False, "not due"
    try:
        _write_private(path, secrets.token_urlsafe(KEY_BYTES))
    except OSError as exc:
        return False, f"rotation failed, old key kept ({type(exc).__name__})"
    return True, "rotated"
