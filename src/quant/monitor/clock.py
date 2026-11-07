"""Startup clock-skew check against an NTP server. Every check is logged to the audit log.

Flow stage: Feed health monitor
Status: PHASE 3 IMPLEMENTED
Existing code to reuse or wrap (do not copy): none (new, flow item 17)

Rules:
- Skew above max_skew_seconds is ERROR and ok=False. The caller must not start trading.
- If the server cannot be reached, the result is WARNING and ok=False (fail closed).
- The check never sends orders.
"""
from __future__ import annotations

import socket
import struct
import time
from typing import Callable, Optional

NTP_EPOCH_OFFSET = 2208988800  # seconds between 1900 (NTP) and 1970 (Unix)


def sntp_time(server: str = "pool.ntp.org", timeout: float = 3.0) -> float:
    """Ask an NTP server for the current Unix time (seconds, float). Raises on failure."""
    packet = b"\x1b" + 47 * b"\0"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(packet, (server, 123))
        data, _ = sock.recvfrom(48)
    secs, frac = struct.unpack("!II", data[40:48])
    return secs - NTP_EPOCH_OFFSET + frac / 2**32


def check_clock(
    fetch_server_time: Callable[[], float],
    audit=None,
    alerts=None,
    max_skew_seconds: float = 1.0,
) -> dict:
    """Compare local time with server time. Returns {status, skew_seconds, ok}."""
    try:
        server = fetch_server_time()
    except Exception as exc:  # network errors vary; any failure fails closed
        result = {"status": "unavailable", "skew_seconds": None, "ok": False, "error": str(exc)}
        level = "WARNING"
    else:
        skew = time.time() - server
        ok = abs(skew) <= max_skew_seconds
        result = {
            "status": "ok" if ok else "skewed",
            "skew_seconds": round(skew, 3),
            "ok": ok,
        }
        level = "INFO" if ok else "ERROR"
    if audit is not None:
        audit.append("clock_check", result)
    if alerts is not None:
        alerts.emit(level, "clock", f"status={result['status']} skew={result['skew_seconds']}")
    return result
