"""
pov_terminal.py — session-authenticated config endpoint for the embedded
POV Dash terminal (React page at /pov-terminal).

The React page renders an <iframe> filling OpenAlgo's content area; the
Dash app itself runs as a separate process/container (port 8050 by
default, or the POV_DASH container in docker-compose). This blueprint
only tells the frontend where that app lives — nothing is proxied, so
Dash keeps its own Plotly callbacks and websockets end to end.

Set POV_DASH_URL in .env when the dashboard runs on another host/port
(e.g. the pov-dash container on a remote box).
"""

from __future__ import annotations

import os

from flask import Blueprint, jsonify

from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

pov_terminal_bp = Blueprint("pov_terminal_bp", __name__)


def _dash_url() -> str:
    """Base URL of the Dash terminal, from env with a local default."""
    return os.getenv("POV_DASH_URL", "http://127.0.0.1:8050").rstrip("/")


@pov_terminal_bp.route("/pov-terminal/config")
@check_session_validity
def pov_terminal_config():
    """Return {url, embedded} for the React iframe page."""
    return jsonify({"url": _dash_url(), "embedded": True})
