"""Edit the broker and OpenAlgo secrets in the repo-root .env from the dashboard.

Owner decision 2026-11-24 (item 3): the owner enters the broker password and
TOTP secret once in the dashboard; they are saved to .env.

Safety rules
------------
* Only the keys in EDITABLE_KEYS can change. Session and API-token keys are not
  editable here, so the dashboard cannot lock the owner out.
* The API never returns a value, only whether each key is set.
* An empty value in an update means "keep the saved value".
* Values may not contain a newline or NUL character, and are length-capped.
* The file is written atomically with owner-only permissions (0600).
* Saved values are also put into os.environ, because the process environment
  takes priority over .env in the secrets resolver.
* Only key names reach the audit log, never values.
* Encryption at rest is NOT done here: the key would sit on the same machine.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

from quant.core.env import DEFAULT_ENV_FILE

EDITABLE_KEYS = (
    "OPENALGO_URL",
    "OPENALGO_API_KEY",
    "OPENALGO_USERNAME",
    "OPENALGO_PASSWORD",
    "OPENALGO_TOTP_SECRET",
    "BROKER_NAME",
    "BROKER_USERNAME",
    "BROKER_PASSWORD",
    "BROKER_TOTP_SECRET",
    "BROKER_AUTOLOGIN",
)
MAX_VALUE_LEN = 512
_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PLAIN_RE = re.compile(r"^[A-Za-z0-9_./:@+=,\-]+$")


class EnvEditError(ValueError):
    """A save was refused. The message names the key, never the value."""


def _check_value(key: str, value: str) -> None:
    if key not in EDITABLE_KEYS:
        raise EnvEditError(f"{key} is not editable from the dashboard")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise EnvEditError(f"{key}: a line break or NUL character is not allowed")
    if len(value) > MAX_VALUE_LEN:
        raise EnvEditError(f"{key}: longer than {MAX_VALUE_LEN} characters")


def _format_line(key: str, value: str) -> str:
    if _PLAIN_RE.match(value):
        return f"{key}={value}"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"'


def _read_lines(path: Path) -> List[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _key_of(line: str) -> Optional[str]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def status(path: Path = DEFAULT_ENV_FILE) -> Dict[str, bool]:
    """For each editable key: is it set in the file or the process environment? No values."""
    saved = {}
    for line in _read_lines(path):
        key = _key_of(line)
        if key and key in EDITABLE_KEYS:
            raw = line.split("=", 1)[1].strip()
            saved[key] = bool(raw.strip('"').strip("'"))
    return {key: bool(saved.get(key) or os.environ.get(key)) for key in EDITABLE_KEYS}


def apply_updates(updates: Mapping[str, str], path: Path = DEFAULT_ENV_FILE) -> List[str]:
    """Save the non-empty values. Returns the key names that changed. Refuses the whole save on any bad key."""
    clean: Dict[str, str] = {}
    for key, value in updates.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise EnvEditError("keys and values must be text")
        if not _KEY_RE.match(key):
            raise EnvEditError(f"{key!r} is not a valid key name")
        _check_value(key, value)
        if value != "":
            clean[key] = value
    if not clean:
        return []

    lines = _read_lines(path)
    written: set = set()
    out: List[str] = []
    for line in lines:
        key = _key_of(line)
        if key in clean:
            out.append(_format_line(key, clean[key]))
            written.add(key)
        else:
            out.append(line)
    for key in clean:
        if key not in written:
            out.append(_format_line(key, clean[key]))

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(out) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    for key, value in clean.items():
        os.environ[key] = value
    return sorted(clean)


def describe_keys(keys: Iterable[str]) -> str:
    """Audit-safe summary: key names only."""
    return ", ".join(sorted(keys))
