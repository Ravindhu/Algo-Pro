"""
start_openalgo.py - Local (no-Docker) launcher for OpenAlgo on Windows.

WHY THIS EXISTS
---------------
OpenAlgo's app.py ends with `socketio.run(app, ...)` without
`allow_unsafe_werkzeug=True`. That is correct for the official Docker
image, which runs gunicorn + eventlet (see start.sh). But a bare venv
never gets eventlet (it is not in requirements.txt - only the Docker
image installs it), so flask-socketio silently falls back to
async_mode="threading", which routes to Flask's Werkzeug dev server,
which flask-socketio then REFUSES in production mode:

    RuntimeError: The Werkzeug web server is not designed to run in
    production. Pass allow_unsafe_werkzeug=True to the run() method...

This launcher fixes the *environment*, not upstream code:
  1. loads .env exactly like app.py does
  2. starts the WebSocket proxy (port 8765) as a separate process
  3. runs the Flask/SocketIO app on the Werkzeug dev server with
     allow_unsafe_werkzeug=True

SAFETY: binds to FLASK_HOST_IP from .env (loopback by default). The
Werkzeug debugger is an RCE primitive - if you ever set
FLASK_HOST_IP=0.0.0.0, this launcher refuses to continue unless you
explicitly pass --allow-external.

USAGE
-----
    openalgo\.venv\Scripts\python.exe my-trading-system\scripts\start_openalgo.py

    # or via the .bat wrapper:
    my-trading-system\scripts\START_OPENALGO.bat
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # my-trading-system/
OPENALGO_DIR = ROOT / "openalgo"
VENV_PY = OPENALGO_DIR / ".venv" / "Scripts" / "python.exe"

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", ""}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Start OpenAlgo locally (no Docker).")
    p.add_argument("--host", default=None, help="Override FLASK_HOST_IP from .env")
    p.add_argument("--port", type=int, default=None, help="Override FLASK_PORT from .env")
    p.add_argument("--no-websocket", action="store_true",
                   help="Skip the standalone WebSocket proxy (app.py starts its own thread)")
    p.add_argument("--allow-external", action="store_true",
                   help="Permit a non-loopback bind (NOT recommended)")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if not OPENALGO_DIR.is_dir():
        print(f"[FAIL] openalgo folder not found at {OPENALGO_DIR}")
        return 1

    # app.py resolves paths relative to CWD, so run from the openalgo folder.
    os.chdir(OPENALGO_DIR)
    sys.path.insert(0, str(OPENALGO_DIR))

    # Load .env with the same loader app.py uses (override=False keeps real env vars).
    try:
        from dotenv import load_dotenv
        load_dotenv(OPENALGO_DIR / ".env")
    except Exception as exc:  # pragma: no cover
        print(f"[FAIL] could not load .env: {exc}")
        return 1

    host = args.host or os.getenv("FLASK_HOST_IP", "127.0.0.1")
    port = args.port or int(os.getenv("FLASK_PORT", "5000"))

    if host not in LOOPBACK_HOSTS and not args.allow_external:
        print(
            "[REFUSED] FLASK_HOST_IP=%r is not loopback. The Werkzeug dev "
            "server exposes an interactive debugger (remote code execution "
            "risk). Re-run with --allow-external only if you accept that risk."
            % host
        )
        return 1

    ws_proc = None
    if not args.no_websocket:
        ws_port = os.getenv("WEBSOCKET_PORT", "8765")
        print(f"[OpenAlgo] Starting WebSocket proxy on port {ws_port} ...")
        ws_proc = subprocess.Popen(
            [str(VENV_PY), "-m", "websocket_proxy.server"],
            cwd=str(OPENALGO_DIR),
        )

    try:
        print(f"[OpenAlgo] Starting web app on http://{host}:{port} ...")
        import app as openalgo_app  # noqa: N813  (import after chdir + dotenv)

        openalgo_app.socketio.run(
            openalgo_app.app,
            host=host,
            port=port,
            debug=True,                    # local dev server; loopback-only
            use_reloader=False,            # a reloader would double-start the WS proxy
            allow_unsafe_werkzeug=True,    # required: no eventlet in a bare venv
        )
    except KeyboardInterrupt:
        print("\n[OpenAlgo] Stopped by user.")
    finally:
        if ws_proc is not None and ws_proc.poll() is None:
            print("[OpenAlgo] Stopping WebSocket proxy ...")
            ws_proc.terminate()
            try:
                ws_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                ws_proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())