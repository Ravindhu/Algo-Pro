"""Save the broker and OpenAlgo secrets from the dashboard, encrypted at rest.

Owner decision 2026-11-24 (item 3, with encryption as recommended): the owner
enters the broker password and TOTP secret once in the dashboard.

Storage
-------
* Values are encrypted with Fernet (AES-128-CBC + HMAC, from the ``cryptography``
  package) into ``data/secrets.enc``. The .env file is NOT written to.
* The Fernet key lives in ``data/secrets.key`` (created on first save, owner-only).
  Keep the whole ``data/`` folder private: anyone with both files can decrypt.
* At dashboard start, :func:`load_saved_secrets` decrypts the store into
  os.environ. A variable already set in the environment or the old .env file wins.

Safety rules
------------
* Only the keys in EDITABLE_KEYS can change. Session and API-token keys are not
  editable here, so the dashboard cannot lock the owner out.
* The API never returns a value, only whether each key is set.
* An empty value in an update means "keep the saved value".
* Values may not contain a newline or NUL character, and are length-capped.
* Writes are atomic and both files are created with owner-only permissions (0600).
* Only key names reach the audit log, never values. Errors never show a value.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

from quant.core.env import DEFAULT_ENV_FILE, REPO_ROOT

_log = logging.getLogger("quant.core.env_editor")

DATA_DIR = REPO_ROOT / "data"
STORE_PATH = DATA_DIR / "secrets.enc"
KEY_PATH = DATA_DIR / "secrets.key"

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
_KEY_NAME_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


class EnvEditError(ValueError):
    """A save was refused. The message names the key, never the value."""


def _fernet_cls():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # fail closed: never store secrets unencrypted
        raise EnvEditError("encryption library (cryptography) is not installed") from exc
    return Fernet


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + "-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _load_or_create_key(key_path: Path) -> bytes:
    if key_path.is_file():
        return key_path.read_bytes().strip()
    key = _fernet_cls().generate_key()
    _write_private(key_path, key + b"\n")
    return key


def _read_store(store: Path, key_path: Path) -> Dict[str, str]:
    """Decrypted values. An unreadable or wrong-key store reads as empty (fails closed)."""
    if not store.is_file():
        return {}
    try:
        fernet = _fernet_cls()(_load_or_create_key(key_path))
        values = json.loads(fernet.decrypt(store.read_bytes()).decode("utf-8"))
        return {k: v for k, v in values.items() if k in EDITABLE_KEYS and isinstance(v, str)}
    except Exception as exc:  # noqa: BLE001 - a bad store must not crash the dashboard
        _log.warning("saved secrets could not be read (%s); treating them as not set", type(exc).__name__)
        return {}


def _check_value(key: str, value: str) -> None:
    if key not in EDITABLE_KEYS:
        raise EnvEditError(f"{key} is not editable from the dashboard")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise EnvEditError(f"{key}: a line break or NUL character is not allowed")
    if len(value) > MAX_VALUE_LEN:
        raise EnvEditError(f"{key}: longer than {MAX_VALUE_LEN} characters")


def _key_name_ok(key: str) -> bool:
    return bool(key) and key[0].isalpha() and key[0].isupper() and set(key) <= _KEY_NAME_OK


def status(store: Path = STORE_PATH, key_path: Path = KEY_PATH,
           env_file: Path = DEFAULT_ENV_FILE) -> Dict[str, bool]:
    """For each editable key: is it set (saved, in the environment, or in the old .env)? No values."""
    saved = _read_store(store, key_path)
    legacy = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key, raw = stripped.split("=", 1)
                legacy[key.strip()] = raw.strip().strip('"').strip("'")
    return {key: bool(saved.get(key) or legacy.get(key) or os.environ.get(key))
            for key in EDITABLE_KEYS}


def apply_updates(updates: Mapping[str, str], store: Path = STORE_PATH,
                  key_path: Path = KEY_PATH) -> List[str]:
    """Save the non-empty values, encrypted. Returns the key names that changed.

    Refuses the whole save if any key or value is bad.
    """
    clean: Dict[str, str] = {}
    for key, value in updates.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise EnvEditError("keys and values must be text")
        if not _key_name_ok(key):
            raise EnvEditError(f"{key!r} is not a valid key name")
        _check_value(key, value)
        if value != "":
            clean[key] = value
    if not clean:
        return []

    fernet = _fernet_cls()(_load_or_create_key(key_path))
    values = _read_store(store, key_path)
    values.update(clean)
    token = fernet.encrypt(json.dumps(values, sort_keys=True).encode("utf-8"))
    _write_private(store, token)
    for key, value in clean.items():
        os.environ[key] = value
    return sorted(clean)


def load_saved_secrets(store: Path = STORE_PATH, key_path: Path = KEY_PATH) -> List[str]:
    """Put the saved secrets into os.environ at start. Existing variables win. Returns key names set."""
    loaded = []
    for key, value in _read_store(store, key_path).items():
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return sorted(loaded)


def describe_keys(keys: Iterable[str]) -> str:
    """Audit-safe summary: key names only."""
    return ", ".join(sorted(keys))
