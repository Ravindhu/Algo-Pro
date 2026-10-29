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

#: Repository root (…/my-trading-system): where the operator's .env lives.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DOTENV_LOADED = False


def load_env_file(path: str | None = None) -> bool:
    """Load the repository-root ``.env`` into ``os.environ`` (without override).

    AUDIT (2026-10-29, LIVE-10): nothing in the Blue_engine runtime ever called
    ``load_dotenv``. The operator's ``OPENALGO_API_KEY`` lived only in the
    root ``.env``, so ``resolve_api_key()`` returned ``None``, the SDK client
    was built with ``api_key=None`` and every live call would have failed with
    an authentication error *after* the process had already started. Loading it
    here - once, before any client is built - makes the documented setup
    (README/SOP: "set OPENALGO_API_KEY in the repository-root .env") true.

    Existing process variables always win: an explicit shell export must never
    be silently replaced by a stale file value.

    Returns True when a file was found and parsed.
    """
    global _DOTENV_LOADED
    target = path or os.path.join(_REPO_ROOT, ".env")
    if not os.path.isfile(target):
        return False
    try:
        from dotenv import load_dotenv
    except Exception:  # pragma: no cover - dotenv is a project dependency
        logger.warning("python-dotenv is not installed; %s was not loaded", target)
        return False
    try:
        loaded = bool(load_dotenv(target, override=False))
        _DOTENV_LOADED = _DOTENV_LOADED or loaded
        return loaded
    except Exception as exc:  # noqa: BLE001 - never break client init
        logger.warning("could not load %s: %s", target, exc)
        return False


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
    if not env_key and not _DOTENV_LOADED:
        # The operator's documented location for the key. Without this the
        # client is built with api_key=None and fails only at the first call.
        load_env_file()
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
    # Load the operator's .env before reading host/ws_url, so a deployment that
    # only configures the file (not the shell) works as documented.
    if not _DOTENV_LOADED:
        load_env_file()
    key = api_key or os.getenv("OPENALGO_API_KEY") or resolve_api_key()
    if not key:
        # Fail at construction, not at the first order: a client with no key is
        # an authentication failure waiting to happen mid-execution.
        raise RuntimeError(
            "no OpenAlgo API key: set OPENALGO_API_KEY in the repository-root "
            ".env (or pass api_key) before starting the engine")
    return api(
        api_key=key,
        host=host or os.getenv("HOST_SERVER", "http://127.0.0.1:5000"),
        ws_url=ws_url or os.getenv("WEBSOCKET_URL", "ws://127.0.0.1:8765"),
    )


#: The ONLY intervals the OpenAlgo history API accepts. Confirmed live against
#: the running server, which answers HTTP 400 with this exact list otherwise:
#: "Must be one of: 1s, 5s, 10s, 15s, 30s, 45s, 1m, 2m, 3m, 5m, 10m, 15m, 20m,
#: 30m, 1h, 2h, 3h, 4h, D, W, M, Q, Y."
VALID_INTERVALS = frozenset({
    "1s", "5s", "10s", "15s", "30s", "45s",
    "1m", "2m", "3m", "5m", "10m", "15m", "20m", "30m",
    "1h", "2h", "3h", "4h",
    "D", "W", "M", "Q", "Y",
})

#: Legacy/verbose spellings seen in older configs and the docs. AUDIT
#: (2026-10-29, LIVE-12): ``master.json`` shipped ``"15minute"``/``"5minute"``,
#: which the API rejects with 400, so the OCL strategy could never load a
#: candle and simply waited forever.
_INTERVAL_ALIASES = {
    "1minute": "1m", "2minute": "2m", "3minute": "3m", "5minute": "5m",
    "10minute": "10m", "15minute": "15m", "20minute": "20m",
    "30minute": "30m", "60minute": "1h", "1hour": "1h",
    "2hour": "2h", "3hour": "3h", "4hour": "4h",
    "1day": "D", "day": "D", "daily": "D", "1d": "D",
    "1week": "W", "week": "W", "weekly": "W", "1w": "W",
    "1month": "M", "month": "M", "monthly": "M", "1mo": "M",
    "quarter": "Q", "yearly": "Y", "1year": "Y",
}


def normalize_interval(value: object) -> str:
    """Map a config interval to the API's vocabulary.

    Valid values pass through unchanged; known legacy spellings are translated;
    anything else is returned as-is so the API's own error names it, rather
    than silently substituting a different timeframe (which would change the
    strategy's behaviour without the operator knowing).
    """
    if value is None:
        return "5m"
    text = str(value).strip()
    if text in VALID_INTERVALS:
        return text
    return _INTERVAL_ALIASES.get(text.lower(), text)
