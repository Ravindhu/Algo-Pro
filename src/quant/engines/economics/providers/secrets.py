"""
providers/secrets.py - the ONLY place credentials are resolved.

Security contract (from AGENTS.md / README.md)
----------------------------------------------
* Secrets are NEVER hardcoded and NEVER committed.
* Resolution order is fixed:
      1. process environment (``os.environ``)  - highest priority
      2. ``<repo>/_restored/.env``             - the single source of truth
      3. ``<repo>/.env``                       - docker-compose shared env
* Only the *name* of an env var appears in source or docs.
* Secrets are read lazily and cached in-process; :func:`redact` guarantees a
  key never leaks into a log line.

The ``_restored/`` location is significant: it is a *sibling* of the pristine
``openalgo/`` clone, so it survives a platform re-clone (see README.md).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional

# Blue_engine/economics/providers/secrets.py -> repo root is 3 parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENV_CANDIDATES = (
    _REPO_ROOT / "_restored" / ".env",
    _REPO_ROOT / ".env",
)


def _parse_env_file(path: Path) -> Dict[str, str]:
    """Minimal dotenv parser (no dependency). Returns {} if unreadable."""
    out: Dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    out[key] = val
    except Exception:  # noqa: BLE001 - a missing .env is normal, not an error
        return {}
    return out


@lru_cache(maxsize=None)
def _file_env() -> Dict[str, str]:
    """Merge the candidate .env files once (cached)."""
    merged: Dict[str, str] = {}
    for path in _ENV_CANDIDATES:
        if path.is_file():
            merged.update(_parse_env_file(path))
    return merged


def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve a secret by env-var *name*. Never returns an empty string.

    Order: environment -> _restored/.env -> repo/.env -> default.
    """
    val = os.environ.get(name)
    if val:
        return val
    val = _file_env().get(name)
    if val:
        return val
    return default


def has_secret(name: str) -> bool:
    """True when a non-empty credential is available for ``name``."""
    return bool(get_secret(name))


def redact(name: str, value: Optional[str]) -> str:
    """Return a safe, loggable representation of a secret.

    Shows only the env-var name and, at most, the last 2 chars so an operator
    can confirm *which* key is loaded without leaking the key.
    """
    if not value:
        return f"{name}=<unset>"
    tail = value[-2:] if len(value) > 6 else ""
    return f"{name}=***{tail} (<{len(value)} chars>)"


def clear_cache() -> None:
    """Drop the parsed-.env cache (used by tests that mutate the environment)."""
    _file_env.cache_clear()


__all__ = ["get_secret", "has_secret", "redact", "clear_cache"]