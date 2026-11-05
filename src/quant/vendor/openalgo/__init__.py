# -*- coding: utf-8 -*-
"""
OpenAlgo Python Library
"""

from .base import BaseAPI
from .orders import OrderAPI
from .data import DataAPI
from .account import AccountAPI
from .strategy import Strategy
from .feed import FeedAPI
from .options import OptionsAPI
from .telegram import TelegramAPI
from .whatsapp import WhatsAppAPI
from .utilities import UtilitiesAPI
from .indicators import ta

# ------------------------------------------------------------------
# Indicator math runs in the Rust core (openalgo._oaindicators); numba/llvmlite
# are no longer dependencies. The shim below keeps legacy decorator imports working.
# ------------------------------------------------------------------
from .numba_shim import jit as _jit_shim, prange as _prange, HAS_NUMBA  # noqa: E402

# Back-compat aliases for any user code that referenced these.
nbjit = _jit_shim
prange = _prange

class api(OrderAPI, DataAPI, AccountAPI, FeedAPI, OptionsAPI, TelegramAPI, WhatsAppAPI, UtilitiesAPI):
    """
    OpenAlgo API client class
    """
    def __init__(self, api_key, host="http://127.0.0.1:5000", version="v1", timeout=120.0,
                 ws_port=8765, ws_url=None, verbose=False, auto_reconnect=True):
        """
        Initialize the OpenAlgo API client.

        Args:
            api_key (str): User's API key.
            host (str): Base URL for the API endpoints. Defaults to localhost.
            version (str): API version. Defaults to "v1".
            timeout (float): Request timeout in seconds. Defaults to 120.0 seconds.
            ws_port (int): WebSocket server port. Defaults to 8765.
            ws_url (str, optional): Custom WebSocket URL. Overrides host and ws_port.
            verbose (int): Logging verbosity level. Defaults to False.
                - 0 or False: Silent mode (errors only)
                - 1 or True: Basic info (connection, auth, subscription status)
                - 2: Full debug (all market data updates)
            auto_reconnect (bool): If True (default), the SDK transparently
                reconnects, re-authenticates, and replays all active
                subscriptions after a drop with exponential backoff. Set to
                False to keep the previous manual-reconnect behaviour.
        """
        # Initialize BaseAPI for REST functionality
        BaseAPI.__init__(self, api_key, host, version, timeout)

        # Initialize FeedAPI WebSocket attributes
        self.verbose = int(verbose) if verbose is not False else 0
        self.ws_port = ws_port

        if ws_url:
            self.ws_url = ws_url
        else:
            if host.startswith("http://"):
                self.ws_host = host[7:]
            elif host.startswith("https://"):
                self.ws_host = host[8:]
            else:
                self.ws_host = host
            self.ws_host = self.ws_host.split('/')[0].split(':')[0]
            self.ws_url = f"ws://{self.ws_host}:{self.ws_port}"

        self.ws = None
        self.connected = False
        self.authenticated = False
        self.ws_thread = None

        # Message management
        import threading as _threading
        self.message_queue = []
        self.lock = _threading.Lock()

        # Data storage
        self.ltp_data = {}
        self.quotes_data = {}
        self.depth_data = {}

        # Callback registry
        self.ltp_callback = None
        self.quote_callback = None
        self.quotes_callback = None
        self.depth_callback = None
        self.order_callback = None

        # Order-update storage (see FeedAPI.subscribe_orders)
        self.order_data = []
        self._orders_subscribed = False

        # Auto-reconnect state (additive — does not change any existing
        # public attribute or behaviour). Mirrors FeedAPI.__init__ so the
        # unified `api()` class supports the same auto-reconnect feature
        # as a standalone FeedAPI instance.
        self.auto_reconnect = bool(auto_reconnect)
        self._shutting_down = False
        self._reconnect_thread = None
        self._reconnect_lock = _threading.Lock()
        self._active_subs = {1: {}, 2: {}, 3: {}}

__version__ = "2.0.3"

# Export main components for easy access
__all__ = ['api', 'Strategy', 'ta', 'nbjit', 'prange', 'HAS_NUMBA']
