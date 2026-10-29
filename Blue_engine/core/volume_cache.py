"""volume_cache.py - WebSocket-backed live volume cache for POV execution.

Responsibility: delivers live broker volume to the POV slicer.
The mstock WebSocket (mode 2/3 binary packets) carries real volume;
REST quotes() returns volume=0. Two transport paths:
  1. Direct mstock WS via platform auth DB (preferred).
  2. SDK REST fallback (degrades gracefully).
"""
from __future__ import annotations

import json
import logging
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import websocket

logger = logging.getLogger(__name__)

_MSTOCK_EXCHANGE_TYPE: Dict[str, int] = {
    "NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4,
    "CDS": 13, "MCX": 5, "NSE_INDEX": 1,
    "BSE_INDEX": 3, "MCX_INDEX": 6,
}

# ------------------------------------------------------------------
# Thread-safe in-process cache
# ------------------------------------------------------------------
@dataclass
class _QuoteSnapshot:
    ltp: float = 0.0
    volume: int = 0
    bid: float = 0.0
    ask: float = 0.0
    bids: list = field(default_factory=list)
    asks: list = field(default_factory=list)
    oi: int = 0
    ts: float = 0.0

_CACHE: Dict[str, _QuoteSnapshot] = {}
_CACHE_LOCK = threading.Lock()

# ------------------------------------------------------------------
# Binary packet parsing
# ------------------------------------------------------------------
def _parse_123_byte_packet(data: bytes) -> Optional[Dict[str, Any]]:
    if len(data) < 123: return None
    try:
        token = data[2:27].decode("utf-8", errors="ignore").rstrip("\x00").strip()
        ltp = struct.unpack("<Q", data[43:51])[0] / 100.0
        volume = struct.unpack("<Q", data[67:75])[0]
        return {"token": token, "ltp": ltp, "volume": volume}
    except Exception: return None

def _parse_379_byte_packet(data: bytes) -> Optional[Dict[str, Any]]:
    if len(data) < 347: return None
    try:
        token = data[2:27].decode("utf-8", errors="ignore").rstrip("\x00").strip()
        ltp = struct.unpack("<Q", data[43:51])[0] / 100.0
        volume = struct.unpack("<Q", data[67:75])[0]
        oi = struct.unpack("<Q", data[131:139])[0]
        bids, asks = [], []
        for off in range(0, 100, 20):
            bp = 147 + off
            if bp + 16 <= len(data):
                p = struct.unpack("<Q", data[bp:bp + 8])[0] / 100.0
                q = struct.unpack("<Q", data[bp + 8:bp + 16])[0]
                if p > 0: bids.append({"price": p, "quantity": q})
        for off in range(0, 100, 20):
            ap = 247 + off
            if ap + 16 <= len(data):
                p = struct.unpack("<Q", data[ap:ap + 8])[0] / 100.0
                q = struct.unpack("<Q", data[ap + 8:ap + 16])[0]
                if p > 0: asks.append({"price": p, "quantity": q})
        return {"token": token, "ltp": ltp, "volume": volume, "oi": oi, "bids": bids, "asks": asks}
    except Exception: return None

def _update_cache(token: str, parsed: Dict[str, Any]) -> None:
    now = time.time()
    with _CACHE_LOCK:
        if token not in _CACHE: _CACHE[token] = _QuoteSnapshot()
        s = _CACHE[token]
        s.ltp = parsed.get("ltp", s.ltp)
        s.volume = parsed.get("volume", s.volume)
        s.oi = parsed.get("oi", s.oi)
        s.bids = parsed.get("bids", s.bids)
        s.asks = parsed.get("asks", s.asks)
        s.bid = s.bids[0]["price"] if s.bids else 0.0
        s.ask = s.asks[0]["price"] if s.asks else 0.0
        s.ts = now

def _resolve_token(symbol: str, exchange: str) -> Optional[str]:
    try:
        from database.token_db import get_token
        return str(get_token(symbol, exchange))
    except Exception: return None

# ------------------------------------------------------------------
# WebSocket background thread
# ------------------------------------------------------------------
_WS_THREAD: Optional[threading.Thread] = None
_WS_RUNNING = False
_WS_LOCK = threading.Lock()

def _ws_connect_and_subscribe(auth_token: str, api_key: str,
                                tokens: Dict[str, str]) -> None:
    global _WS_RUNNING
    ws_url = ("wss://ws.mstock.trade"
              "?API_KEY=" + api_key + "&ACCESS_TOKEN=" + auth_token)

    def on_open(ws):
        ws.send("LOGIN:" + auth_token)
        # Subscribe each token only on its own exchange type. Sending every
        # token to every exchange type produced invalid subscriptions and
        # stale/conflicting cache entries.
        token_exchange = {
            tok: ex for key, tok in tokens.items()
            for ex in [key.split("_")[-1]]
        }
        for tok, ex in token_exchange.items():
            exchange_type = _MSTOCK_EXCHANGE_TYPE.get(ex, 1)
            for mode in [2, 3]:
                msg = json.dumps({
                    "action": 1,
                    "params": {
                        "mode": mode,
                        "tokenList": [
                            {"exchangeType": exchange_type, "tokens": [tok]}
                        ],
                    },
                })
                ws.send(msg)

    def on_message(_ws, msg):
        if not isinstance(msg, (bytes, bytearray)): return
        parsed = None
        if len(msg) == 123:
            parsed = _parse_123_byte_packet(msg)
        elif len(msg) >= 347:
            # The 379-byte parser already accepts any packet of at least 347
            # bytes; gating on >=379 here silently dropped 347-378 byte
            # packets that carry valid volume.
            parsed = _parse_379_byte_packet(msg)
        if parsed and parsed.get("token"):
            _update_cache(parsed["token"], parsed)

    ws_app = None
    try:
        ws_app = websocket.WebSocketApp(
            ws_url,
            on_open=on_open, on_message=on_message,
            on_error=lambda _w, e: logger.warning("mstock WS error: %s", e),
            on_close=lambda _w, sc, m: logger.info(
                "mstock WS closed code=%s msg=%s", sc, m),
        )
        ws_app.run_forever(ping_interval=20, ping_timeout=10)
    except Exception as exc:
        logger.error("mstock WS thread crashed: %s", exc)
    finally:
        global _WS_RUNNING
        _WS_RUNNING = False


# SDK REST fallback
def _sdk_fallback(client, symbol, exchange):
    """Two-tier fallback:
    1. Direct broker get_history (low latency, returns volume)
    2. OpenAlgo SDK history (slower, via API, returns volume)
    3. Zero-volume error response (last resort)
    """
    import os
    from datetime import datetime, timedelta
    import importlib

    # ------------------------------------------------------------------
    # 1. Try direct broker get_history first (lowest latency)
    # ------------------------------------------------------------------
    try:
        from database.auth_db import get_auth_token_broker
        api_key = os.getenv("OPENALGO_API_KEY", "")
        auth_token, feed_token, broker_name = get_auth_token_broker(
            api_key, include_feed_token=True
        )

        if not (auth_token and broker_name):
            raise ValueError("Missing auth token or broker name")

        # Import broker module directly
        broker_module = importlib.import_module(
            f"broker.{broker_name}.api.data"
        )

        # Last 10 minutes, 1-minute bars (same as POV slicer's interval).
        # Naive UTC (utcnow-equivalent) because broker history APIs take naive
        # timestamps. ``timezone.utc`` not ``datetime.UTC``: the latter is 3.11+
        # only and this engine also runs on 3.10.
        end = datetime.now(timezone.utc).replace(tzinfo=None)
        start = end - timedelta(minutes=10)

        # Initialize broker data handler with auth token (and optional feed token)
        param_count = broker_module.BrokerData.__init__.__code__.co_argcount
        if param_count > 2:  # More than self and auth_token
            data_handler = broker_module.BrokerData(auth_token, feed_token)
        else:
            data_handler = broker_module.BrokerData(auth_token)

        # Call broker's get_history method directly (low latency)
        df = data_handler.get_history(
            symbol=symbol,
            exchange=exchange,
            interval="1m",
            start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
            end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
        )

        # Extract volume from broker response
        if hasattr(df, "to_dict"):
            records = df.to_dict(orient="records")
        elif isinstance(df, dict):
            records = df.get("data", [])
        else:
            records = []

        if records:
            total_volume = sum(int(r.get("volume", 0)) for r in records)
            if total_volume > 0:
                latest = records[-1]
                return {
                    "status": "success",
                    "data": {
                        "ltp": float(latest.get("close", 0)),
                        "volume": total_volume,
                        "bid": 0.0,
                        "ask": 0.0,
                        "bids": [],
                        "asks": [],
                        "oi": int(latest.get("oi", 0)),
                        "open": float(latest.get("open", 0)),
                        "high": float(latest.get("high", 0)),
                        "low": float(latest.get("low", 0)),
                        "prev_close": float(latest.get("close", 0)),
                        "_source": "broker_history",
                    },
                }

    except Exception as exc:
        logger.warning(
            "volume_cache: direct broker history failed (%s); trying SDK fallback",
            exc,
        )

    # ------------------------------------------------------------------
    # 2. Fallback to OpenAlgo SDK history
    # ------------------------------------------------------------------
    try:
        if client is not None:
            end = datetime.now(timezone.utc).replace(tzinfo=None)
            start = end - timedelta(minutes=10)

            # SDK history method exists in client.history()
            history_data = client.history(
                symbol=symbol,
                exchange=exchange,
                interval="1m",
                start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
                api_key=os.getenv("OPENALGO_API_KEY", ""),
                source="api",
            )

            if isinstance(history_data, dict) and history_data.get("status") == "success":
                records = history_data.get("data", [])
                if records:
                    total_volume = sum(int(r.get("volume", 0)) for r in records)
                    if total_volume > 0:
                        latest = records[-1]
                        return {
                            "status": "success",
                            "data": {
                                "ltp": float(latest.get("close", 0)),
                                "volume": total_volume,
                                "bid": 0.0,
                                "ask": 0.0,
                                "bids": [],
                                "asks": [],
                                "oi": int(latest.get("oi", 0)),
                                "open": float(latest.get("open", 0)),
                                "high": float(latest.get("high", 0)),
                                "low": float(latest.get("low", 0)),
                                "prev_close": float(latest.get("close", 0)),
                                "_source": "sdk_history",
                            },
                        }

    except Exception as exc:
        logger.warning(
            "volume_cache: SDK history fallback failed (%s)",
            exc,
        )

    # ------------------------------------------------------------------
    # 3. Last resort: return zeros
    # ------------------------------------------------------------------
    return {
        "status": "error",
        "message": "volume_cache: no volume data available",
        "data": {
            "ltp": 0,
            "bid": 0,
            "ask": 0,
            "volume": 0,
            "oi": 0,
            "open": 0,
            "high": 0,
            "low": 0,
            "prev_close": 0,
        },
    }

# Public API
_SUBSCRIBED = False
#: Symbols the current subscription was built from (diagnostics + reuse).
_SUBSCRIBE_SYMBOLS: List[Any] = []

def _resolve_run_symbols(symbols) -> Dict[str, str]:
    """Map ``"SYMBOL_EXCHANGE" -> broker token`` for the symbols we must stream.

    Pure lookups against the platform token DB; an unresolvable symbol is
    omitted (logged), never replaced by a default token.
    """
    resolved: Dict[str, str] = {}
    if not symbols:
        return resolved
    try:
        from database.token_db import token_db  # type: ignore
    except Exception as exc:
        logger.warning("volume_cache: token DB unavailable (%s)", exc)
        return resolved
    for item in symbols:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            symbol, exchange = str(item[0]).strip(), str(item[1]).strip().upper()
        else:
            symbol, exchange = str(item).strip(), "NSE"
        if not symbol:
            continue
        try:
            token = token_db.get(symbol, exchange)
        except Exception as exc:  # noqa: BLE001
            logger.debug("volume_cache: %s/%s lookup failed: %s",
                         symbol, exchange, exc)
            continue
        if token:
            resolved[f"{symbol}_{exchange}"] = str(token)
        else:
            logger.warning("volume_cache: no broker token for %s/%s",
                           symbol, exchange)
    return resolved


def subscribe(client=None, symbols=None):
    """Start the background broker WebSocket thread. Idempotent.

    ``symbols`` is the run's symbol list (``"INFY"`` or ``("INFY", "NSE")``).
    Only those tokens are streamed: the cache must never claim to know a
    symbol the engine was not asked to trade.
    Returns: True = WS active, False = REST fallback.
    """
    global _SUBSCRIBED, _WS_THREAD, _WS_RUNNING, _SUBSCRIBE_SYMBOLS
    if _SUBSCRIBED:
        if symbols:
            # A second run in this process may need a different symbol set;
            # remember it for diagnostics without re-opening the socket.
            _SUBSCRIBE_SYMBOLS = list(symbols)
        return _WS_RUNNING
    _SUBSCRIBE_SYMBOLS = list(symbols or [])
    with _WS_LOCK:
        if _SUBSCRIBED: return True
        try:
            from database.auth_db import get_auth_token_broker
            auth_token, _ = get_auth_token_broker(os.getenv("OPENALGO_API_KEY", ""))
            api_key = os.getenv("BROKER_API_SECRET") or os.getenv("BROKER_API_KEY", "")
        except Exception as exc:
            logger.warning("volume_cache: cannot read platform auth DB (%s); degrading to REST fallback", exc)
            auth_token = None
        if auth_token and api_key:
            # AUDIT (2026-10-29, P0-5): this used to subscribe a hardcoded
            # INFY/TCS/RELIANCE list and then fall back to the token "1594"
            # (INFY on NSE). Any other symbol therefore streamed nothing, the
            # cache missed, and the POV loop sliced on the REST fallback with
            # volume=0 - i.e. it never sliced at all while reporting success.
            # The subscription is now driven by the caller's run config, and a
            # symbol that cannot be resolved is NOT guessed.
            _resolved = _resolve_run_symbols(list(symbols or []))
            if not _resolved:
                logger.warning(
                    "volume_cache: no symbol could be resolved to a broker "
                    "token; live volume will come from the REST fallback")
                _SUBSCRIBED = True
                return False
            _WS_RUNNING = True
            _WS_THREAD = threading.Thread(target=_ws_connect_and_subscribe,
                args=(auth_token, api_key, _resolved),
                name="pov-volume-cache-ws", daemon=True)
            _WS_THREAD.start()
            _SUBSCRIBED = True
            logger.info("volume_cache: mstock WS started for %d symbols: %s",
                        len(_resolved), list(_resolved.values()))
            return True
        logger.warning("volume_cache: direct WS unavailable; REST fallback (volume will be 0)")
        _SUBSCRIBED = True
        return False

def get_quote(symbol, exchange, client=None):
    """Return latest cached quote. Same dict shape as client.quotes().
    Falls back to REST when WS cache is empty.
    """
    token = _resolve_token(symbol, exchange)
    with _CACHE_LOCK:
        if token and token in _CACHE:
            snap = _CACHE[token]
            if time.time() - snap.ts < 60:
                return {
                    "status": "success",
                    "data": {
                        "ltp": snap.ltp,
                        "bid": snap.bid,
                        "ask": snap.ask,
                        "bids": snap.bids,
                        "asks": snap.asks,
                        "volume": snap.volume,
                        "oi": snap.oi,
                        "open": 0.0, "high": 0.0, "low": 0.0, "prev_close": 0.0,
                    },
                }
    if client is not None:
        return _sdk_fallback(client, symbol, exchange)
    return {"status": "error", "message": "volume_cache: no live data and no SDK client",
            "data": {"ltp": 0, "bid": 0, "ask": 0, "volume": 0, "oi": 0,
                     "open": 0, "high": 0, "low": 0, "prev_close": 0}}

def cache_snapshot():
    """Return a copy of the cache for diagnostics."""
    with _CACHE_LOCK:
        return {
            token: {
                "ltp": s.ltp, "volume": s.volume,
                "bid": s.bid, "ask": s.ask,
                "bids": list(s.bids), "asks": list(s.asks),
                "oi": s.oi,
                "age_s": round(time.time() - s.ts, 1) if s.ts else None,
            }
            for token, s in _CACHE.items()
        }

__all__ = ["subscribe", "get_quote", "cache_snapshot"]
