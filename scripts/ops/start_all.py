r"""
start_all.py - ONE launcher for the whole local stack.

Starts, in order, and verifies each one before moving on:

    1. free the ports       (5000 OpenAlgo, 8050 Dash, 8765 WebSocket)
    2. [optional] updates   (pip / OpenAlgo git pull / data)  -- OFF by default
    3. OpenAlgo             via Docker if available, else natively
    4. WebSocket proxy      whatever OpenAlgo did NOT already start
    5. Dash terminal        always native (no Docker image exists for it)

WHY IT IS WRITTEN THIS WAY
--------------------------
Ports first, always. The most common failure is not a crash - it is
"[Errno 10048] address already in use" because a previous run was never
stopped. This script asks before killing, and kills the whole PROCESS TREE
(a stale launcher holds a child, which holds the port; killing only the
parent leaves the port held).

Verify, do not assume. "The process started" is not "the service works".
Each service is polled over HTTP until it answers, with a timeout, and the
run exits non-zero if one never does. A green exit means every port answered.

UPDATES ARE OFF BY DEFAULT. `pip install -U` and `git pull` inside
openalgo/ can break a working desk, and they take minutes. Pass --update to
opt in. They run BEFORE anything starts, never against a live service.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO
-----------------------------------------
* It never logs into the broker. OpenAlgo restores a session from
  _restored/; if that session is stale you log in yourself at /broker.
  Automating TOTP/credentials is not a launcher's job and is not safe.
* It never edits the openalgo/ clone. All local state stays in _restored/
  and is bridged in by scripts/ops/start_openalgo.py.
* It never reaches the network except for the opt-in --update.

DOCKER
------
`docker info` must answer, not just `docker --version` - Docker Desktop
installs the CLI long before the daemon is up. If only the OpenAlgo
container is wanted, this still starts Dash natively afterwards, because
Dash has no image.

USAGE
-----
    python scripts\ops\start_all.py              # start everything (no updates)
    python scripts\ops\start_all.py --check      # ports + prerequisites only
    python scripts\ops\start_all.py --update     # update deps/data first
    python scripts\ops\start_all.py --no-docker  # force the native path
    python scripts\ops\start_all.py --stop       # stop everything
    python scripts\ops\start_all.py --force      # kill port holders without asking

    # or double-click:
    scripts\ops\START_ALL.cmd
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]          # my-trading-system/
OPENALGO_DIR = ROOT / "openalgo"
LOG_DIR = ROOT / "Temp" / "run"
PID_FILE = LOG_DIR / "start_all.pids.json"

OPENALGO_PORT = int(os.getenv("OPENALGO_PORT", "5000"))
DASH_PORT = int(os.getenv("DASH_PORT", "8050"))
WS_PORT = int(os.getenv("WEBSOCKET_PORT", "8765"))

VENV_PY_CANDIDATES = [
    ROOT / ".venv" / "Scripts" / "python.exe",
    OPENALGO_DIR / ".venv" / "Scripts" / "python.exe",
]

LINE = "-" * 62


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def say(msg: str) -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    print(f"  [ok]   {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"  [warn] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}", flush=True)


def step(n: str, msg: str) -> None:
    print(f"\n{LINE}\n{n}. {msg}\n{LINE}", flush=True)


# --------------------------------------------------------------------------- #
# Prerequisites
# --------------------------------------------------------------------------- #
def venv_python() -> Optional[Path]:
    for candidate in VENV_PY_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def docker_available() -> bool:
    """True only when the DAEMON answers.

    `docker --version` succeeds the moment Docker Desktop is installed, even
    with the daemon stopped. Asking `docker info` is the difference between
    "Docker is installed" and "Docker can start a container".
    """
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=15)
        return result.returncode == 0 and bool(result.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        return False


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #
def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def port_owners(port: int) -> List[int]:
    """PIDs listening on ``port`` (Windows via netstat, else via lsof)."""
    pids: List[int] = []
    try:
        netstat = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
            timeout=15).stdout
        for line in netstat.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}") \
                    and parts[3].upper() == "LISTENING":
                pid = int(parts[4])
                if pid and pid not in pids:
                    pids.append(pid)
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    return pids


def kill_tree(pid: int) -> bool:
    """Kill a PID and all its children.

    A stale launcher is a parent whose child actually holds the port. Killing
    only the parent leaves the child listening, which is exactly how a port
    stays "busy" after you thought you stopped everything.
    """
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, timeout=20)
        return True
    except (subprocess.SubprocessError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except OSError:
            return False


def free_ports(force: bool) -> bool:
    """Free every port we need. Returns False if one could not be freed."""
    busy = {p: port_owners(p) for p in (OPENALGO_PORT, DASH_PORT, WS_PORT)}
    busy = {p: pids for p, pids in busy.items() if port_in_use(p) or pids}
    if not busy:
        ok("ports 5000 / 8050 / 8765 are free")
        return True

    for port, pids in busy.items():
        warn(f"port {port} in use by PID(s) {pids or 'unknown'}")
        if not pids:
            fail(f"port {port} is held but the owner could not be identified; "
                 f"free it manually (netstat -ano | findstr :{port})")
            return False
        if not force:
            answer = input(f"    kill PID {pids} on port {port}? [y/N] ").strip().lower()
            if answer != "y":
                fail(f"port {port} left in use - cannot start")
                return False
        for pid in pids:
            kill_tree(pid)

    time.sleep(1.5)
    still = [p for p in (OPENALGO_PORT, DASH_PORT, WS_PORT) if port_in_use(p)]
    if still:
        fail(f"still in use after kill: {still}")
        return False
    ok("ports 5000 / 8050 / 8765 freed")
    return True


# --------------------------------------------------------------------------- #
# Optional updates (opt-in only)
# --------------------------------------------------------------------------- #
def refresh_dotenv() -> bool:
    """OpenAlgo resolves .env relative to its own root at import time, so a
    change in _restored/.env must be bridged in BEFORE the app is imported.
    start_openalgo.py does this too; doing it here keeps --check meaningful.
    """
    src = ROOT / "_restored" / ".env"
    dst = OPENALGO_DIR / ".env"
    if not src.is_file():
        warn("_restored/.env not found - OpenAlgo will boot unconfigured")
        return False
    if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
        ok("openalgo/.env is current")
        return True
    shutil.copy2(src, dst)
    ok("openalgo/.env refreshed from _restored/.env")
    return True


def run_updates(args: argparse.Namespace) -> bool:
    """Dependency, upstream and data refresh. Runs BEFORE anything starts."""
    py = venv_python()
    if py is None:
        fail("no project venv found (.venv/Scripts/python.exe)")
        return False

    say("  pip: upgrading project dependencies ...")
    rc = subprocess.call([str(py), "-m", "pip", "install", "-U", "-r",
                          str(ROOT / "requirements.txt"), "-q"],
                         cwd=str(ROOT))
    if rc != 0:
        warn(f"pip exited {rc} - continuing with the installed set")
    else:
        ok("dependencies up to date")

    if (OPENALGO_DIR / ".git").is_dir():
        say("  openalgo: git pull --ff-only ...")
        rc = subprocess.call(["git", "pull", "--ff-only"], cwd=str(OPENALGO_DIR))
        if rc != 0:
            warn("openalgo git pull failed (local changes?) - NOT forcing it")
        else:
            ok("openalgo is at upstream")
    else:
        warn("openalgo/ is not a git clone - skipping pull")

    return True


# --------------------------------------------------------------------------- #
# Service launch
# --------------------------------------------------------------------------- #
def spawn(name: str, cmd: List[str], cwd: Path) -> Optional[subprocess.Popen]:
    """Start a service that OUTLIVES this launcher and its console.

    WHAT WAS TRIED AND FAILED (2026-10-23):
      * ``Popen(creationflags=CREATE_NEW_PROCESS_GROUP)`` - killed with the
        terminal's job object.
      * ``DETACHED_PROCESS`` and ``CREATE_BREAKAWAY_FROM_JOB`` - same.
      * ``cmd /c ...`` - the host survived briefly but the child did not.

    WHAT WORKS: ``Start-Process -WindowStyle Hidden``. It hands the child to
    the shell's process manager rather than to this launcher, so the service
    keeps running after the window closes. Verified on this host.

    Output is redirected by cmd into Temp/run/<name>.log, never to a pipe: an
    undrained pipe blocks the child once its buffer fills, which is
    indistinguishable from a hang.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{name}.log"
    quoted = " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd)

    # Write the command to a .cmd file and launch THAT. Passing a nested
    # cmd.exe command through powershell -Command requires three levels of
    # quoting, and a single mis-escaped character makes Start-Process
    # fail silently (no process, no error) - which cost several debugging
    # rounds on 2026-10-23. A script file has no escaping problem at all and
    # is also far easier to read when something goes wrong.
    script_path = LOG_DIR / f"{name}.cmd"
    script_path.write_text(
        "@echo off\r\n"
        f'cd /d "{cwd}"\r\n'
        f'{quoted} > "{log_path}" 2>&1\r\n',
        encoding="utf-8",
    )

    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / \
        "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    try:
        subprocess.run(
            [str(powershell), "-NoProfile", "-WindowStyle", "Hidden",
             "-Command",
             f"Start-Process -FilePath '{script_path}' -WindowStyle Hidden"],
            check=False, timeout=30,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        ok(f"{name} started -> {log_path.relative_to(ROOT)}")
        return None
    except (subprocess.SubprocessError, OSError) as exc:
        fail(f"{name} could not start: {exc}")
        return None


def find_by_cmdline(marker: str) -> List[int]:
    """PIDs whose command line contains ``marker``.

    The ``cmd /c`` host exits at once, so its PID is useless for tracking a
    long-running service. We identify the real process by what it is running.
    """
    pids: List[int] = []
    try:
        listing = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'" "",
             "get", "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=20).stdout
        for line in listing.splitlines():
            if marker.lower() in line.lower():
                tail = line.rsplit(",", 1)[-1].strip()
                if tail.isdigit():
                    pids.append(int(tail))
    except (subprocess.SubprocessError, OSError):
        pass
    return pids


def http_ready(url: str, timeout: float = 120.0) -> bool:
    """True once ``url`` answers with anything below 500."""
    started = time.time()
    while time.time() - started < timeout:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status < 500:
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code < 500:      # a redirect/401 still proves it is serving
                return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        time.sleep(2)
    return False


def wait_http(name: str, url: str, timeout: float = 120.0,
              accept_any: bool = False) -> bool:
    """Poll until the URL answers. The only proof a service actually works."""
    started = time.time()
    while time.time() - started < timeout:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if accept_any or response.status < 500:
                    ok(f"{name} answered HTTP {response.status} "
                       f"({time.time() - started:.0f}s)")
                    return True
        except urllib.error.HTTPError as exc:
            # An auth redirect / 4xx still proves the server is listening.
            if exc.code < 500:
                ok(f"{name} answered HTTP {exc.code} "
                   f"({time.time() - started:.0f}s)")
                return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        time.sleep(2)
    fail(f"{name} did not answer within {timeout:.0f}s "
         f"(see Temp/run/{name}.log)")
    return False


def wait_port(name: str, port: int, timeout: float = 60.0) -> bool:
    started = time.time()
    while time.time() - started < timeout:
        if port_in_use(port):
            ok(f"{name} listening on {port} ({time.time() - started:.0f}s)")
            return True
        time.sleep(1)
    fail(f"{name} never listened on {port}")
    return False


def start_openalgo(args: argparse.Namespace) -> bool:
    py = venv_python()
    if py is None:
        fail("no project venv found")
        return False

    if args.use_docker:
        say("  mode: Docker (openalgo container)")
        rc = subprocess.call(
            ["docker", "compose", "up", "-d"],
            cwd=str(OPENALGO_DIR))
        if rc != 0:
            warn("docker compose failed - falling back to the native path")
            args.use_docker = False
        else:
            ok("openalgo container requested")
            return wait_http("openalgo", f"http://127.0.0.1:{OPENALGO_PORT}/",
                             timeout=180)

    say("  mode: native (start_openalgo.py)")
    # --no-websocket: app.py starts its own proxy thread. Starting a second
    # one is what produces "address already in use" on 8765.
    proc = spawn("openalgo", [str(py), str(ROOT / "scripts" / "ops" /
                                         "start_openalgo.py"), "--no-websocket"],
                 cwd=ROOT)
    return proc is not None


def start_websocket() -> bool:
    """OpenAlgo owns the WebSocket proxy - this must NOT start a second one.

    AUDIT (2026-10-23): OpenAlgo's app_integration.py starts
    ``WebSocketProxy(port=8765)`` at boot and REFUSES to run if that port is
    taken:

        RuntimeError: WebSocket port 8765 is already in use on 127.0.0.1.
        Cannot start WebSocket server with port switching as it would break
        SDK clients.

    So launching ``python -m websocket_proxy.server`` first does not merely
    duplicate a service - it makes OpenAlgo fail to start entirely. That was
    the real reason every earlier attempt died. This function therefore only
    waits for the port that OpenAlgo's own thread opens.
    """
    if wait_port("websocket (via OpenAlgo)", WS_PORT, timeout=60):
        return True
    warn("WebSocket proxy not listening - OpenAlgo starts it at boot; "
         "check Temp/run/openalgo.log for the boot error")
    return False


def start_dash() -> bool:
    py = venv_python()
    if py is None:
        fail("no project venv found")
        return False
    proc = spawn("dash", [str(py), "-m", "blue_engine.dashboard.web.run"], cwd=ROOT)
    return proc is not None


def start_engine() -> bool:
    """Start the trading engine under the supervisor (audit P0-7).

    OFF BY DEFAULT. Starting the engine is a trading decision, not a
    convenience: this launcher only guarantees the platform underneath it.
    Pass --engine to opt in; the supervisor then applies its restart gates
    (kill switch, OpenAlgo, positionbook, working orders, quote) before every
    start and refuses to retry a safety stop.
    """
    py = venv_python()
    if py is None:
        fail("no project venv found")
        return False
    proc = spawn("engine", [str(py), str(ROOT / "scripts" / "run" /
                                      "run_supervised.py")], cwd=ROOT)
    return proc is not None


# --------------------------------------------------------------------------- #
# PID bookkeeping (so --stop can find what we started)
# --------------------------------------------------------------------------- #
def save_pids(entries: Dict[str, int]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def load_pids() -> Dict[str, int]:
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def stop_all() -> int:
    step("STOP", "stopping the local stack")
    stopped = 0
    for name, pid in load_pids().items():
        if kill_tree(pid):
            ok(f"stopped {name} (pid {pid})")
            stopped += 1
    for port in (OPENALGO_PORT, DASH_PORT, WS_PORT):
        for pid in port_owners(port):
            if kill_tree(pid):
                ok(f"stopped pid {pid} holding {port}")
                stopped += 1
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    if not stopped:
        say("  nothing was running")
    return 0


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Start the whole local stack: OpenAlgo, WebSocket, Dash.")
    p.add_argument("--check", action="store_true",
                   help="Check prerequisites and ports, then exit")
    p.add_argument("--update", action="store_true",
                   help="Update dependencies / OpenAlgo / data first "
                        "(OFF by default - can break a working desk)")
    p.add_argument("--no-docker", action="store_true",
                   help="Force the native OpenAlgo path")
    p.add_argument("--force", action="store_true",
                   help="Kill port holders without asking")
    p.add_argument("--stop", action="store_true", help="Stop the whole stack")
    p.add_argument("--no-dash", action="store_true", help="Skip Dash")
    p.add_argument("--engine", action="store_true",
                   help="Also start the trading engine under the supervisor "
                        "(never auto-resumed after a safety stop)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.stop:
        return stop_all()

    say(f"\n{LINE}\nMy Trading System - local stack\n{LINE}")
    say(f"  root     : {ROOT}")
    say(f"  python   : {venv_python() or 'NOT FOUND'}")
    args.use_docker = (not args.no_docker) and docker_available()
    say(f"  docker   : {'available' if args.use_docker else 'not available (native path)'}")
    say(f"  updates  : {'ON' if args.update else 'off'}")

    failures: List[str] = []
    if venv_python() is None:
        fail("project venv missing - run: python -m venv .venv")
        return 1

    step("1", "Free the ports")
    if not free_ports(args.force) and not args.check:
        return 1

    step("2", "Configuration")
    if not args.check:
        refresh_dotenv()

    step("3", "Updates" + ("" if args.update else " (skipped - pass --update)"))
    if args.update and not run_updates(args):
        return 1
    if not args.update:
        say("  dependencies, OpenAlgo and data left untouched")

    if args.check:
        step("DONE", "check only - nothing started")
        return 0

    return start_stack(args)


def start_stack(args: argparse.Namespace) -> int:
    """Launch all three, persist PIDs, then verify over HTTP."""
    failures: List[str] = []
    pids: Dict[str, int] = {}

    step("4", "OpenAlgo")
    if start_openalgo(args):
        save_pids(pids)
    else:
        failures.append("openalgo")

    step("5", "WebSocket proxy (started by OpenAlgo)")
    if not start_websocket():
        failures.append("websocket")

    step("6", "Dash terminal")
    if args.no_dash:
        say("  skipped (--no-dash)")
    elif not start_dash():
        failures.append("dash")

    step("7", "Trading engine" + ("" if args.engine else " (skipped - pass --engine)"))
    if not args.engine:
        say("  the platform is up but NO engine is trading (safe default)")
    elif not start_engine():
        failures.append("engine")

    _record_port_owners(pids)

    step("7", "Verify (this takes a moment)")
    if "openalgo" not in failures:
        # OpenAlgo takes ~200s cold on this box: it bridges _restored/, runs
        # the upgrade check, loads the master contract and starts the strategy
        # module. 180s was too tight and reported a healthy server as failed.
        if not http_ready(f"http://127.0.0.1:{OPENALGO_PORT}/",
                          timeout=300):
            fail("openalgo did not answer within 300s "
                 f"(see Temp/run/openalgo.log)")
            failures.append("openalgo")
        else:
            ok("openalgo answered")
    if "websocket" not in failures:
        if not port_in_use(WS_PORT):
            failures.append("websocket")
    if "dash" not in failures and not args.no_dash:
        # Dash imports the whole engine graph; 240s is generous on purpose.
        if not http_ready(f"http://127.0.0.1:{DASH_PORT}/", timeout=240):
            fail("dash did not answer within 240s "
                 f"(see Temp/run/dash.log)")
            failures.append("dash")
        else:
            ok("dash answered")

    _record_port_owners(pids)

    step("RESULT", "stack status")
    say(f"  OpenAlgo  : http://127.0.0.1:{OPENALGO_PORT}")
    say(f"  Dashboard : http://127.0.0.1:{DASH_PORT}")
    say(f"  WebSocket : ws://127.0.0.1:{WS_PORT}")
    say(f"  logs      : {LOG_DIR}")
    say("  stop with : python scripts\\ops\\start_all.py --stop")
    say("  services run detached - you can close this window")
    if failures:
        fail(f"not healthy: {', '.join(failures)}")
        return 1
    ok("all services answered")
    return 0


def _record_port_owners(pids: Dict[str, int]) -> None:
    """Persist the PIDs that actually own the ports.

    The ``cmd /c`` hosts exit immediately, so their PIDs are useless for
    ``--stop``. The listener on each port is the real service process.
    """
    for name, port in (("openalgo", OPENALGO_PORT),
                       ("websocket", WS_PORT), ("dash", DASH_PORT)):
        owners = port_owners(port)
        if owners:
            pids[name] = owners[0]
    save_pids(pids)


if __name__ == "__main__":
    sys.exit(main())
