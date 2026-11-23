"""order_channel.py - the one test an engine uses before it sends an order.

Flow stage: OpenAlgo
Status: PHASE 1 IMPLEMENTED

Engines never import the order client. The caller injects a client that is
either:
  * a guarded EngineOrderGateway (built in execution/openalgo_client.py, marked
    ``is_order_gateway``), which runs the live / analyze guard before each send; or
  * the paper broker (marked ``is_paper_broker``), which never reaches OpenAlgo.
Any other client is refused. This helper only checks the marker. The guard itself
lives in the gateway.
"""
from __future__ import annotations

from typing import Any


def order_channel_ok(client: Any) -> bool:
    """True only for a guarded order gateway or the paper broker."""
    return (getattr(client, "is_order_gateway", False) is True
            or getattr(client, "is_paper_broker", False) is True)
