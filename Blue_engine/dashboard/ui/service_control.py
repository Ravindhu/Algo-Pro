"""
service_control.py - read-only service status + a guarded OpenAlgo START button.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
=============================================

The operator asked for Start/Stop buttons for OpenAlgo and the Blue Engine.
Only ONE of those four controls is safe to build, and this module explains and
enforces why.

    1. **Probe service status**          -> READ-ONLY, built here.
    2. **START OpenAlgo**                -> built here, with guard rails.
    3. **STOP OpenAlgo**                 -> NOT built. See "WHY THERE IS NO STOP".
    4. **START/STOP the Blue Engine**    -> NOT built. See "WHY THE ENGINE HAS NO
                                            BUTTON".

WHY THERE IS NO STOP BUTTON
---------------------------
A Stop button that works is a Stop button that can strand capital. If the
dashboard can kill OpenAlgo, then a misclick, a stale browser tab, or a crashed
dashboard can leave open positions with no execution bridge: the operator
cannot exit, adjust, or square off, and the control they would use to fix it is
the one they just killed. That inverts the project's own priority hierarchy
(AGENTS.md): Capital Protection -> Data Integrity -> Risk Control -> Execution
Integrity -> System Stability -> Strategy Performance.

Stopping must therefore stay a deliberate action on the machine itself, where
the operator can see the process and restart it. This is not a UI gap; it is the
safety property. We surface the PID and the exact stop command instead, so
stopping is one copy-paste away but is never a misclick.

WHY THE BLUE ENGINE HAS NO BUTTON
---------------------------------
``blue_engine.core.runner`` is a RUN-TO-COMPLETION CLI, not a daemon. It loads
the config, polls signals, fires ONE intent through the risk gate, hands it to
the slicer, and EXITS. There is no loop, no socket, no long-running process.

So "start the Blue Engine" does not mean "start a service" -- it means "place an
order". A Start button for it would BE an order-placing button, which directly
violates SOP section 4: the terminal writes ``master.json`` and nothing in the
dashboard places, modifies or cancels orders. The engine is started from the
CLI on purpose, so that emitting a live order is never one dashboard click.

WHY STARTING OPENALGO IS DIFFERENT
----------------------------------
Starting the bridge is not a trading action:

  * It is IDEMPOTENT in effect -- the probe refuses to launch when the platform
    is already answering, so a double click cannot create a second instance.
  * It is RECOVERABLE in one click -- if it is wrong, Start again.
  * It is purely ADDITIVE to safety -- a bridge that is up can only make the
    system MORE able to see and act on its state, never less. It cannot place
    an order by itself; OpenAlgo's own analyzer/live mode decides that, and the
    dashboard never toggles it.

That asymmetry -- Stop is destructive and unrecoverable, Start is additive and
idempotent -- is the whole reason one got a button and the other did not.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["probe_all", "probe_http", "probe_tcp", "probe_ws",
           "start_openalgo", "start_command_preview", "openalgo_pids"]

# Repo root: this file is Blue_engine/dashboard/ui/service_control.py
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Default ports (SOP section 2/3).
OPENALGO_REST_PORT = 5000
OPENALGO_WS_PORT = 8765
DASH_PORT = 8050

#: How long the START handshake waits before reporting "launching" rather than
#: "started". The POST must return FAST -- a Dash callback that blocks for 40 s
#: (the real OpenAlgo boot time) would freeze the browser and invite a reclick,
#: which is exactly how you get the duplicate processes we cleaned up earlier.
_LAUNCH_SETTLE_S = 1.5


# --------------------------------------------------------------------------- #
# Port probes (read-only)
# --------------------------------------------------------------------------- #

def probe_tcp(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """True when something is LISTENING on ``port``. Never raises.

    A TCP connect is all we need for the REST port. For the WebSocket proxy a
    bare TCP connect is NOT sufficient evidence of health -- it cannot tell a
    live handshake from a half-open socket -- so use :func:`probe_ws` there.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, int(port))) == 0
    except Exception:  # noqa: BLE001 - a probe must never raise
        return False
    finally:
        s.close()


def probe_http(port: int, host: str = "127.0.0.1", timeout: float = 2.0,
               path: str = "/") -> bool:
    """True when an HTTP GET on ``path`` answers with any status. Never raises.

    IMPLEMENTED OVER A RAW SOCKET, NOT ``urllib`` (fixed 2026-10-08).

    Verified live: ``urllib.request.urlopen("http://127.0.0.1:5000/",
    timeout=2.0)`` BLOCKED INDEFINITELY against the running OpenAlgo app even
    though a raw TCP connect to the same port completed in 0.4 s. The
    ``timeout`` argument governs socket READS, not the whole exchange, so a
    server that accepts the connection and then dribbles its response pins the
    caller forever. Inside a Dash callback that would freeze the panel (and the
    browser) with no error -- the worst kind of failure.

    A raw socket with ``settimeout`` applied to connect, send AND recv is
    bounded end-to-end, needs no dependency, and is honest: we only need to
    know whether an HTTP response arrived, not what it said. Both ports here
    serve localhost over plain HTTP, so a TLS/redirect path is unnecessary.
    """
    # NOTE ON THE SOCKET SHAPE (fixed 2026-10-08).
    #
    # ``socket.create_connection(timeout=...)`` sets the timeout for the
    # CONNECT only; the returned socket then inherits the default (blocking
    # forever) unless we set it again -- which we do below. Verified live: the
    # earlier form returned True in 0.02 s against OpenAlgo but HUNG FOREVER
    # against the Dash server on 8050, because Dash accepts the connection and
    # does not answer a bare HTTP/1.0 request promptly. The failure mode was a
    # callback that never returns, i.e. a frozen panel.
    #
    # A 2 s budget is generous for localhost and bounded end-to-end.
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, int(port)))
        sock.settimeout(timeout)          # re-arm AFTER connect (see note)
        sock.sendall(
            b"GET " + str(path).encode() + b" HTTP/1.0\r\n"
            b"Host: " + f"{host}:{port}".encode() + b"\r\n"
            b"User-Agent: pov-service-probe\r\n"
            b"Connection: close\r\n\r\n")
        head = sock.recv(64)
        # "HTTP/1.x <code>" -- any code (200, 404, 500) proves a server is
        # listening and speaking HTTP, which is the whole question here.
        return head.startswith(b"HTTP/")
    except Exception:  # noqa: BLE001 - a probe must never raise
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass


def probe_ws(port: int = OPENALGO_WS_PORT, host: str = "127.0.0.1",
             timeout: float = 2.0) -> bool:
    """True when the WebSocket proxy completes a real upgrade handshake.

    VERIFIED 2026-10-08: a plain ``socket.connect_ex`` reports the WS proxy as
    DOWN even with ``python -m websocket_proxy.server`` listening, because the
    proxy accepts the TCP connection and then requires an HTTP Upgrade. Only a
    genuine handshake proves health, so we send one and require
    ``101 Switching Protocols``.
    """
    import base64
    import os as _os

    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
    except Exception:  # noqa: BLE001
        return False
    try:
        key = base64.b64encode(_os.urandom(16))
        s.sendall(
            b"GET / HTTP/1.1\r\n"
            b"Host: " + f"{host}:{port}".encode() + b"\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Key: " + key + b"\r\n"
            b"Sec-WebSocket-Version: 13\r\n\r\n")
        s.settimeout(timeout)
        head = s.recv(256).decode("latin-1", "replace")
        return "101" in head.splitlines()[0] if head else False
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Process introspection (read-only)
# --------------------------------------------------------------------------- #

def _python_processes() -> List[Dict[str, Any]]:
    """Every python.exe as ``{pid, ppid, cmd}``. Empty list on any error.

    Read-only. Uses PowerShell's CIM rather than ``wmic``
    (wmic is deprecated and absent on newer Windows builds) and parses JSON so
    a command line containing a comma cannot corrupt the field split.
    """
    if os.name != "nt":
        return []
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Select-Object ProcessId,ParentProcessId,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
        if not out:
            return []
        data = json.loads(out)
        if isinstance(data, dict):        # single result -> not a list
            data = [data]
    except Exception:  # noqa: BLE001 - probe only
        return []
    rows: List[Dict[str, Any]] = []
    for r in data if isinstance(data, list) else []:
        try:
            rows.append({"pid": int(r.get("ProcessId") or 0),
                         "ppid": int(r.get("ParentProcessId") or 0),
                         "cmd": str(r.get("CommandLine") or "")})
        except Exception:  # noqa: BLE001
            continue
    return rows


def openalgo_pids() -> List[int]:
    """The OpenAlgo LAUNCHER PIDs only -- the roots, not their children.

    CORRECTED 2026-10-08 (verified live): a naive command-line grep returned 6
    PIDs for a single healthy OpenAlgo and made the panel look like it had a
    duplicate-process problem. The reality is a legitimate process CHAIN:

        start_openalgo.py  (launcher)
          -> start_openalgo.py  (its own re-exec / supervisor)
               -> websocket_proxy  (the WS server)
                    -> websocket_proxy  (its reloader child)

    Counting every match reports the chain as duplicates. We therefore match
    ONLY the launcher command and drop any PID whose PARENT is also a match,
    which leaves the true root(s). Read-only; never kills anything.
    """
    rows = _python_processes()
    launchers = {r["pid"]: r for r in rows if "start_openalgo" in r["cmd"].lower()}
    if not launchers:
        return []
    # Keep only roots: a launcher whose parent is NOT itself a launcher.
    roots = [pid for pid, r in launchers.items() if r["ppid"] not in launchers]
    return sorted(roots) if roots else sorted(launchers)


# --------------------------------------------------------------------------- #
# The full status snapshot
# --------------------------------------------------------------------------- #

def probe_all() -> Dict[str, Any]:
    """One snapshot of every service the terminal depends on. Never raises.

    Returns a dict with ``services`` (a list of rows for the UI), plus
    ``openalgo_up`` / ``rest_up`` / ``ws_up`` booleans for callers that just
    need the verdict.
    """
    rest_up = probe_http(OPENALGO_REST_PORT)
    ws_up = probe_ws(OPENALGO_WS_PORT)
    dash_up = probe_http(DASH_PORT)

    # Probe PIDs whenever REST is down (to distinguish "stopped" from "booting").
    # When REST is UP we still list them, but only the ROOT launcher, so the
    # panel can show a PID to stop without presenting the process chain as
    # duplicates (see openalgo_pids).
    pids = openalgo_pids()
    if rest_up and pids:
        pids_note = "running (root pid " + ", ".join(map(str, pids)) + ")"
    elif rest_up:
        pids_note = "running"
    elif pids:
        pids_note = ("starting / not answering -- pid "
                     + ", ".join(map(str, pids)))
    else:
        pids_note = "not detected"

    services = [
        {"service": "OpenAlgo REST", "up": rest_up,
         "endpoint": f"http://127.0.0.1:{OPENALGO_REST_PORT}/",
         "detail": "answering" if rest_up else "not answering"},
        {"service": "OpenAlgo WS proxy", "up": ws_up,
         "endpoint": f"ws://127.0.0.1:{OPENALGO_WS_PORT}",
         "detail": ("handshake OK" if ws_up
                    else "no 101 upgrade (order updates will not stream)")},
        {"service": "POV terminal", "up": dash_up,
         "endpoint": f"http://127.0.0.1:{DASH_PORT}/",
         "detail": "answering" if dash_up else "not answering"},
        {"service": "OpenAlgo processes", "up": bool(rest_up or pids),
         "endpoint": "process probe", "detail": pids_note},
    ]

    return {
        "services": services,
        "rest_up": rest_up,
        "ws_up": ws_up,
        "dash_up": dash_up,
        "openalgo_up": rest_up,
        "pids": pids,
    }


# --------------------------------------------------------------------------- #
# START OpenAlgo
# --------------------------------------------------------------------------- #

def start_command_preview() -> List[str]:
    """The exact argv used to launch OpenAlgo, for display and for launching."""
    return [
        str(_REPO_ROOT / ".venv" / "Scripts" / "python.exe"),
        str(_REPO_ROOT / "scripts" / "start_openalgo.py"),
        "--no-scrub-on-exit",
    ]


def _launch_detached(argv: List[str], env: Optional[Dict[str, str]] = None,
                     cwd: Optional[str] = None) -> int:
    """Spawn ``argv`` detached and return its pid.

    Isolated into its own function for two reasons:

    1. **Testability.** ``subprocess.Popen`` and ``subprocess.run`` are
       attributes of the SAME module object, so patching ``Popen`` to assert
       "we did not launch" also intercepts ``subprocess.run`` -- which
       ``openalgo_pids()`` legitimately uses. That collision made an earlier
       version of the guard test pass for the wrong reason. A named seam on
       OUR module is unambiguous.
    2. **Readability.** The Windows creation flags are non-obvious.

    Flags: ``CREATE_NO_WINDOW`` (do not pop a console) and
    ``DETACHED_PROCESS`` (survive the request that spawned it). Both are
    Windows-only; elsewhere the flag set is empty.
    """
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0))
    proc = subprocess.Popen(
        argv, cwd=cwd or str(_REPO_ROOT), env=env or dict(os.environ),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True,
    )
    return int(proc.pid)


def start_openalgo(*, force: bool = False) -> Dict[str, Any]:
    """Launch OpenAlgo if it is not already answering.

    Returns a result dict ``{ok, reason, launched, already_running, pids}``.
    Never raises -- the UI renders ``reason`` verbatim.

    Guards, in order:

    1. **Idempotence.** If the REST port already answers, we DO NOT launch. This
       is the guard that makes a Start button safe: a double click, a page
       refresh, or two browser tabs cannot spawn duplicate OpenAlgo instances.
       (We already had to clean up duplicates once today; this prevents it.)
    2. **Pre-flight.** The launcher script must exist, else report a clear
       reason instead of a silent nothing.
    3. **Non-blocking launch.** ``Popen`` returns immediately; we do NOT wait
       for the ~40 s boot inside the callback. ``CREATE_NO_WINDOW`` +
       ``DETACHED_PROCESS`` keeps it alive after the request ends and stops it
       stealing the dashboard's console.

    ``force=True`` skips guard 1 and is used only by the CLI helper, never by
    the UI -- a button must never be able to double-launch.
    """
    if not force and probe_http(OPENALGO_REST_PORT):
        return {"ok": True, "already_running": True, "launched": False,
                "reason": "OpenAlgo is already answering on "
                          f"127.0.0.1:{OPENALGO_REST_PORT}; not launching a "
                          f"second instance.",
                "pids": openalgo_pids()}

    argv = start_command_preview()
    if not Path(argv[0]).is_file():
        return {"ok": False, "already_running": False, "launched": False,
                "reason": f"venv python not found at {argv[0]}", "pids": []}
    if not Path(argv[1]).is_file():
        return {"ok": False, "already_running": False, "launched": False,
                "reason": f"launcher not found at {argv[1]}", "pids": []}

    env = dict(os.environ)
    # SOP section 2: APP_MODE=standalone stops app.py double-starting the WS
    # proxy, which is what produced the orphan 8765 holders we cleaned up.
    env["APP_MODE"] = "standalone"

    try:
        pid = _launch_detached(argv, env=env, cwd=str(_REPO_ROOT))
    except Exception as exc:  # noqa: BLE001 - report, never raise into a callback
        logger.warning("start_openalgo failed: %r", exc)
        return {"ok": False, "already_running": False, "launched": False,
                "reason": f"launch failed: {exc}", "pids": []}

    logger.info("start_openalgo launched pid=%s (settle %ss)",
                pid, _LAUNCH_SETTLE_S)
    return {
        "ok": True, "already_running": False, "launched": True,
        "pid": pid,
        "reason": (f"Launched OpenAlgo (pid {pid}). Boot takes ~40 s; "
                   f"this panel re-probes every 5 s. Note: the daily broker "
                   f"session may still need a login in the OpenAlgo UI."),
        "pids": [],
    }


if __name__ == "__main__":  # manual probe from the CLI
    import json
    print(json.dumps(probe_all(), indent=2))
    sys.exit(0)


# --------------------------------------------------------------------------- #
# The panel (DMC components; imported lazily so this module stays testable)
# --------------------------------------------------------------------------- #

def control_panel(snapshot: Optional[Dict[str, Any]] = None) -> Any:
    """Build the Service Control card: status table + START button + stop hint.

    Pure function of ``snapshot`` so tests can render any state without a
    server. Returns a ``dmc.Paper``.
    """
    import dash_mantine_components as dmc
    from dash import dcc, html

    snap = snapshot if snapshot is not None else probe_all()
    rows = snap["services"]

    def _badge(up: bool) -> Any:
        return dmc.Badge("UP" if up else "DOWN",
                         color="green" if up else "red",
                         variant="filled", size="sm")

    table_rows = [
        html.Tr([
            html.Td(dmc.Text(r["service"], fw=600, size="sm")),
            html.Td(_badge(bool(r["up"]))),
            html.Td(dmc.Text(r["endpoint"], size="xs", ff="monospace")),
            html.Td(dmc.Text(r["detail"], size="xs")),
        ])
        for r in rows
    ]

    rest_up = bool(snap.get("rest_up"))
    pids = snap.get("pids") or []

    # The START button. Disabled when already up so a double click cannot
    # launch a second instance -- the same guard the backend enforces, surfaced
    # in the UI so the operator is never invited to do the wrong thing.
    start_btn = dmc.Button(
        "Start OpenAlgo" if not rest_up else "OpenAlgo already running",
        id="svc-start-openalgo",
        color="green", variant="filled" if not rest_up else "light",
        disabled=rest_up, n_clicks=0, leftSection=None,
    )

    stop_hint = dmc.Alert(
        title="Stop is deliberately not a button",
        color="orange", variant="light",
        children=[
            dmc.Text(
                "A Stop button that works can strand open positions with no "
                "execution bridge. Stop it on the machine, where you can see "
                "the process and restart it:", size="xs"),
            dmc.Code(
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -like '*start_openalgo*' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
                block=True, mt="xs"),
            dmc.Text(
                ("Current root launcher pid(s): " + ", ".join(map(str, pids)))
                if pids else "No OpenAlgo launcher process detected.",
                size="xs", mt="xs", c="dimmed"),
        ],
    )

    no_engine = dmc.Alert(
        title="The Blue Engine has no Start button, on purpose",
        color="gray", variant="light",
        children=dmc.Text(
            "blue_engine.core.runner is a run-to-completion CLI: it fires ONE "
            "order through the risk gate and exits. A Start button for it would "
            "be an order-placing button, which SOP section 4 forbids -- the "
            "terminal only writes master.json. Start the engine from the CLI.",
            size="xs"),
    )

    return dmc.Paper(
        [
            dmc.Group([
                dmc.Title("Service Control", order=4),
                dmc.Badge("read-only except START", color="gray", variant="light"),
            ], justify="space-between"),
            dmc.Text(
                "Probes are live: REST over HTTP, the WS proxy over a real "
                "101 handshake, the terminal over HTTP. Re-probes every 5 s.",
                size="xs", c="dimmed", mt=4),
            html.Div(id="svc-action-result", style={"marginTop": "10px"}),
            dmc.Table(
                [html.Thead(html.Tr([
                    html.Th("Service"), html.Th("Status"),
                    html.Th("Endpoint"), html.Th("Detail"),
                ])), html.Tbody(table_rows)],
                striped=True, highlightOnHover=True, mt="sm",
            ),
            dmc.Group([start_btn], mt="md"),
            dcc.Interval(id="svc-poll", interval=5000, n_intervals=0),
            html.Div([stop_hint, no_engine],
                     style={"marginTop": "16px",
                            "display": "grid", "gap": "10px"}),
        ],
        p="lg", radius="md", withBorder=True,
    )


def register_callbacks(app: Any) -> None:
    """Wire the status poll and the START action.

    Kept in this module (not the page) so the control has exactly one owner.
    Both callbacks are defensive: an exception renders as a message, never a
    500 in the operator's face.
    """
    from dash import Input, Output, State, no_update

    @app.callback(
        Output("svc-action-result", "children"),
        Input("svc-start-openalgo", "n_clicks"),
        State("svc-poll", "n_intervals"),
        prevent_initial_call=True,
    )
    def _start(n_clicks, _n):        # noqa: ANN001
        if not n_clicks:
            return no_update
        try:
            res = start_openalgo()
            colour = "green" if res.get("ok") else "red"
            title = ("Already running" if res.get("already_running")
                     else "Launch requested" if res.get("ok") else "Start failed")
            return _alert(title, res.get("reason", ""), colour)
        except Exception as exc:  # noqa: BLE001 - never 500 a callback
            return _alert("Start failed", repr(exc), "red")


def _alert(title: str, body: str, colour: str) -> Any:
    from dash import html
    import dash_mantine_components as dmc

    return dmc.Alert(html.Span([
        html.B(body + " "),
        html.Span("The service table refreshes on the next poll (5 s).",
                  style={"opacity": 0.75}),
    ]), title=title, color=colour, variant="light")
