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

import os

# The blue_engine/__init__.py module runs _sdkpath first, so this
# import now correctly resolves to the installed openalgo SDK.
from openalgo import api


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
        api_key=api_key or os.getenv("OPENALGO_API_KEY"),
        host=host or os.getenv("HOST_SERVER", "http://127.0.0.1:5000"),
        ws_url=ws_url or os.getenv("WEBSOCKET_URL", "ws://127.0.0.1:8765"),
    )
