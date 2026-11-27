"""Save OpenAlgo's CURRENT API key after a login (owner decision 2026-11-27: read the existing key).

Plain English: after the dashboard signs in to OpenAlgo and the broker, ask OpenAlgo for the
API key it already has, and keep ``OPENALGO_API_KEY`` in ``_restored/.env`` up to date.

Rules:
* Read only on the OpenAlgo side. ``GET /apikey`` returns the current key. Nothing is generated,
  so no existing key is revoked and no other tool that uses the key breaks.
* Write only when ``_restored/.env`` ALREADY EXISTS (it is never created here), and only when
  the saved value differs. Other lines are kept.
* The key value is never logged, never returned in a result and never audited. The outcome is one
  of a few fixed words.

Flow stage: Dashboard sign-in (credential upkeep; no order path)
Status: PHASE 1 IMPLEMENTED (2026-11-27; tests in tests/unit/test_openalgo_apikey.py)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict

from quant.core.env_write import write_env_values

#: Repository root: src/quant/core/ -> three levels up.
REPO_ROOT = Path(__file__).resolve().parents[3]
RESTORED_ENV = REPO_ROOT / "_restored" / ".env"
KEY_NAME = "OPENALGO_API_KEY"
APIKEY_PATH = "/apikey"


def read_env_value(path, name: str) -> str:
    """Value of ``name`` in a .env file. The last assignment wins, as python-dotenv does. Never raises."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    value = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        if key.strip() == name:
            value = raw.strip().strip('"').strip("'")
    return value


def fetch_key(get: Callable[[str], Dict[str, Any]]) -> Dict[str, Any]:
    """The current key from the logged-in OpenAlgo session. ``get(path)`` returns a response dict.

    Returns ``{"key": str | None, "reason": str}``. ``reason`` is a fixed, safe sentence.
    """
    response = get(APIKEY_PATH)
    body = response.get("body") if isinstance(response, dict) else None
    if not (isinstance(response, dict) and response.get("ok") and isinstance(body, dict)):
        return {"key": None, "reason": "OpenAlgo did not answer the API key page"}
    key = body.get("api_key")
    if not body.get("has_api_key") or not isinstance(key, str) or not key.strip():
        return {"key": None, "reason": "no API key yet - create one at /apikey in OpenAlgo"}
    return {"key": key.strip(), "reason": ""}


def sync_env(key: str, env_path=RESTORED_ENV) -> str:
    """Make ``OPENALGO_API_KEY`` in ``env_path`` equal ``key``. Returns a fixed outcome word.

    Outcomes: ``"skipped: _restored/.env not found"`` (the file is never created here),
    ``"unchanged"``, or ``"updated"``.
    """
    target = Path(env_path)
    if not target.is_file():
        return "skipped: _restored/.env not found"
    if read_env_value(target, KEY_NAME) == key:
        return "unchanged"
    write_env_values(target, {KEY_NAME: key})
    return "updated"
