"""
mode.py — read-only access to OpenAlgo's analyzer mode.

IMPORTANT: This module NEVER calls analyzertoggle(). Mode (LIVE vs ANALYZER)
is controlled exclusively in the OpenAlgo UI. This function only reports the
current mode so the strategy can display it for operator awareness.
"""
from typing import Any


def read_mode(client: Any) -> str:
    """Return the current OpenAlgo mode as a string.

    Returns one of: "live", "analyze", or "unknown" if the read fails.
    The value comes from POST /api/v1/analyzer (via client.analyzerstatus()).
    See openalgo/account.py:analyzerstatus for the underlying SDK call.

    Args:
        client: An initialized openalgo.api instance.

    Returns:
        "live"    — orders will go to your broker
        "analyze" — orders will be simulated in the sandbox
        "unknown" — read failed; treat as 'do not assume'
    """
    try:
        resp = client.analyzerstatus()
        if resp.get("status") == "success":
            return resp.get("data", {}).get("mode", "unknown")
    except Exception:
        # Never let a mode-read failure crash the strategy. Operator can
        # verify mode manually in the OpenAlgo UI.
        pass
    return "unknown"
