"""
client.py - initialize the OpenAlgo SDK client.

Mirrors the pattern from the official OpenAlgo reference strategy
(strategies/Custom Statergy/simple_ema_strategy.py):

    from openalgo import api
    api_key = os.getenv('OPENALGO_API_KEY')
    host    = os.getenv('HOST_SERVER', 'http://127.0.0.1:5000')
    ws_url  = os.getenv('WEBSOCKET_URL', 'ws://127.0.0.1:8765')
    client  = api(api_key=api_key, host=host, ws_url=ws_url)

The `blue_engine/__init__.py` runs `blue_engine._sdkpath` first, which
fixes `sys.path` so `from openalgo import api` resolves to the
INSTALLED SDK (openalgo==2.0.3) instead of the empty local platform
package at openalgo/openalgo/__init__.py. So this import is safe.

All arguments are keyword-only, matching the SDK signature in
openalgo/__init__.py (api.__init__).
"""
from __future__ import annotations

import logging
import os

# The blue_engine/__init__.py module runs _sdkpath first, so this
# import now correctly resolves to the installed openalgo SDK.
from openalgo import api

logger = logging.getLogger(__name__)


def resolve_api_key() -> str | None:
    """Resolve the OpenAlgo API key without a hard dependency on OpenAlgo internals.

    Resolution order:
        1. OPENALGO_API_KEY environment variable (explicit override).
        2. Key decrypted from the local OpenAlgo database (openalgo/db/openalgo.db),
           exactly the way the OpenAlgo UI serves it. Caches the result for the
           process lifetime so we do not re-open SQLite on every client build.

    Returns None only when OpenAlgo is genuinely not installed/initialized here
    (e.g. on a research-only box) -- callers then surface the SDK's own error.
    """
    env_key = os.getenv("OPENALGO_API_KEY")
    if env_key:
        return env_key

    global _CACHED_KEY
    if _CACHED_KEY is not None:
        return _CACHED_KEY or None

    key: str | None = None
    try:  # pragma: no cover - exercised only when OpenAlgo db is present
        import sqlite3

        openalgo_root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "openalgo",
        )
        db_path = os.path.join(openalgo_root, "db", "openalgo.db")
        if os.path.isfile(db_path):
            # Load OpenAlgo's .env (API_KEY_PEPPER + FERNET_SALT) so the
            # Fernet key derived by auth_db matches what encrypted the key.
            try:
                from dotenv import load_dotenv

                load_dotenv(os.path.join(openalgo_root, ".env"), override=False)
            except Exception:  # pragma: no cover - dotenv is an OpenAlgo dep
                pass
            # OpenAlgo resolves its data path relative to its own root, so we
            # must chdir/import from there; restore afterwards.
            from database.auth_db import get_api_key_for_tradingview  # noqa: E402

            con = sqlite3.connect(db_path)
            try:
                user_id = (
                    con.execute("select user_id from api_keys limit 1").fetchone()
                )
            finally:
                con.close()
            if user_id:
                key = get_api_key_for_tradingview(user_id[0]) or None
    except Exception as exc:  # pragma: no cover - never break client init
        logger.debug("OpenAlgo API key auto-discovery failed: %s", exc)
        key = None

    _CACHED_KEY = key or ""
    return key


_CACHED_KEY: str | None = None


def init_client(api_key: str | None = None,
                host: str | None = None,
                ws_url: str | None = None):
    """Initialize and return an OpenAlgo SDK client.

    Resolution order for each value:
        1. Argument passed in (highest priority)
        2. Environment variable
        3. SDK default

    Args:
        api_key: OpenAlgo API key. Falls back to OPENALGO_API_KEY env var.
        host:    OpenAlgo REST base URL. Falls back to HOST_SERVER env var.
        ws_url:  OpenAlgo WebSocket URL. Falls back to WEBSOCKET_URL env var.

    Returns:
        An instance of openalgo.api - keyword-only arg compatible with
        placeorder, placesmartorder, quotes, history, modifyorder, cancelorder,
        orderbook, tradebook, positionbook, analyzerstatus, etc.
    """
    return api(
        api_key=api_key or resolve_api_key(),
        host=host or os.getenv("HOST_SERVER", "http://127.0.0.1:5000"),
        ws_url=ws_url or os.getenv("WEBSOCKET_URL", "ws://127.0.0.1:8765"),
    )
