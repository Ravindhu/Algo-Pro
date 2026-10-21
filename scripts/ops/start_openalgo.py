r"""
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
  1. materialises the _restored/ state into the clone (see RESTORE below)
  2. loads .env exactly like app.py does
  3. starts the WebSocket proxy (port 8765) as a separate process
  4. runs the Flask/SocketIO app on the Werkzeug dev server with
     allow_unsafe_werkzeug=True

RESTORE (why _restored/ exists)
-------------------------------The openalgo/ folder must stay a PRISTINE upstream clone so that a
`git pull` / re-clone never conflicts and never needs a hand edit. All
local state therefore lives in a SIBLING folder, my-trading-system/_restored/:

    _restored/
      .env          OpenAlgo config  (FERNET_SALT, API_KEY_PEPPER, DB URLs)
      db/           SQLite databases (broker tokens live here, encrypted)
      keys/         Fernet/oauth key material
      download/     master-contract symbol cache

OpenAlgo resolves .env and the db relative to its OWN root, so a
subfolder is invisible to it. This launcher bridges the gap: before
importing app it copies _restored/* into openalgo/ (only if newer, or
with --force-restore). Nothing is ever edited in the upstream code.

CRITICAL: .env and db/ are cryptographically married. FERNET_SALT and
API_KEY_PEPPER in .env are the KDF inputs that encrypt the broker
session tokens stored in db/openalgo.db. Restore one without the other
and OpenAlgo boots but cannot decrypt your broker login. Always keep
them together - that is why both live in _restored/.

SAFETY: binds to FLASK_HOST_IP from .env (loopback by default). The
Werkzeug debugger is an RCE primitive - if you ever set
FLASK_HOST_IP=0.0.0.0, this launcher refuses to continue unless you
explicitly pass --allow-external.

USAGE
-----
    # Preferred (Docker-style, isolated openalgo venv):
    openalgo\.venv\Scripts\python.exe my-trading-system\scripts\ops\start_openalgo.py

    # Fallback (project venv, when openalgo/.venv has not been created):
    .venv\Scripts\python.exe my-trading-system\scripts\ops\start_openalgo.py

    # or via the .bat wrapper:
    my-trading-system\scripts\START_OPENALGO.bat
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent      # my-trading-system/
OPENALGO_DIR = ROOT / "openalgo"
RESTORED_DIR = ROOT / "_restored"
# Prefer openalgo/.venv (the Docker-style install); fall back to the
# project-level .venv at the repo root when the openalgo venv is missing.
# The project venv already has the openalgo SDK; we just install the
# additional server-side deps (flask-socketio, eventlet, etc.) into it.
_VENV_CANDIDATES = [
    OPENALGO_DIR / ".venv" / "Scripts" / "python.exe",
    ROOT / ".venv" / "Scripts" / "python.exe",
]
VENV_PY = next((p for p in _VENV_CANDIDATES if p.exists()), _VENV_CANDIDATES[0])

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", ""}

# State that must survive a re-clone. Each entry is (source under _restored,
# destination under openalgo). 'download' is optional - it is just a cache.
RESTORE_ITEMS = [
    (".env", ".env"),
    ("db", "db"),
    ("keys", "keys"),
    ("download", "download"),
]

# Files we *scrub* on clean exit. We do NOT scrub db/, keys/, or download/ -
# those contain state that survives the OpenAlgo process (broker sessions,
# master-contract cache) and removing them would force a re-login on the
# next start. The .env is the only file we scrub because it is the only
# file that contains broker credentials in plaintext, and it is re-bootstrapped
# from _restored/.env at the next start.
SCRUB_ON_EXIT = [".env"]


def _newer(src: Path, dst: Path) -> bool:
    """True if src is missing at dst, or src is newer than dst."""
    if not dst.exists():
        return True
    try:
        return src.stat().st_mtime > dst.stat().st_mtime
    except OSError:
        return True


def bootstrap_restored(force: bool = False) -> int:
    """Copy _restored/<item> into openalgo/<item>.

    This is the ONLY bridge between the isolated state folder and the
    pristine clone. It never touches upstream code - it only places
    config/data files that upstream itself expects to be untracked
    (.env and db/ are both in OpenAlgo's own .gitignore).

    Returns 0 on success, non-zero on a hard failure.
    """
    import shutil

    if not RESTORED_DIR.is_dir():
        print(f"[FAIL] _restored/ not found at {RESTORED_DIR}")
        print("       Create it and place .env / db / keys inside, or a fresh")
        print("       OpenAlgo will boot unconfigured (no broker session).")
        return 1

    print(f"[OpenAlgo] Bootstrap: {RESTORED_DIR.name}/ -> {OPENALGO_DIR.name}/")
    for src_name, dst_name in RESTORE_ITEMS:
        src = RESTORED_DIR / src_name
        dst = OPENALGO_DIR / dst_name
        if not src.exists():
            print(f"[OpenAlgo]   - {src_name:<9} absent in _restored (skipped)")
            continue

        if src.is_dir():
            # Copy the folder contents, honouring per-file freshness so a
            # live db written by a running OpenAlgo is not clobbered by a
            # stale restore copy.
            copied = 0
            for child in src.rglob("*"):
                if not child.is_file():
                    continue
                rel = child.relative_to(src)
                target = dst / rel
                if force or _newer(child, target):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(child, target)
                    copied += 1
            print(f"[OpenAlgo]   - {src_name:<9} {copied} file(s) restored")
        else:
            if force or _newer(src, dst):
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                print(f"[OpenAlgo]   - {src_name:<9} restored")
            else:
                print(f"[OpenAlgo]   - {src_name:<9} up to date")

    if not (OPENALGO_DIR / ".env").exists():
        print("[FAIL] openalgo/.env missing after bootstrap - cannot start.\n"
              "       Put your .env in _restored/.env and retry.")
        return 1
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Start OpenAlgo locally (no Docker).")
    p.add_argument("--host", default=None, help="Override FLASK_HOST_IP from .env")
    p.add_argument("--port", type=int, default=None, help="Override FLASK_PORT from .env")
    p.add_argument("--no-websocket", action="store_true",
                   help="Skip the standalone WebSocket proxy (app.py starts its own thread)")
    p.add_argument("--allow-external", action="store_true",
                   help="Permit a non-loopback bind (NOT recommended)")
    p.add_argument("--no-restore", action="store_true",
                   help="Skip the _restored/ bootstrap step")
    p.add_argument("--force-restore", action="store_true",
                   help="Restore every _restored/ file, ignoring timestamps")
    p.add_argument("--strict-pin", action="store_true",
                   help="Refuse to start if the openalgo/ folder does not match the "
                        "upstream pin in openalgo.UPSTREAM_PIN. Use scripts/"
                        "upgrade_check.py --update-pin after a deliberate upgrade.")
    p.add_argument("--no-scrub-on-exit", action="store_true",
                   help="Skip the secret-scrub on exit. Only useful for debugging; "
                        "leaves your mstock secret in openalgo/.env on disk.")
    p.add_argument("--check-only", action="store_true",
                   help="Run the bootstrap + upgrade check, then exit (no app start)")
    return p.parse_args()


def scrub_secrets(only: list[str] | None = None) -> list[str]:
    """Delete the secret-bearing files we placed in openalgo/ at bootstrap.

    The point of `_restored/` is that the .env and any other secret-bearing
    files live ONLY there. If we leave them in openalgo/ after OpenAlgo
    stops, anyone who later runs `git init` inside openalgo/ would stage
    them as a fresh commit. We therefore delete them on clean exit so
    openalgo/ remains a *pristine* upstream copy with no local secrets.

    The next `start_openalgo.py` run will re-bootstrap them from
    `_restored/`, so this is safe to do on every exit.

    Returns the list of files that were actually removed.
    """
    targets = only if only is not None else SCRUB_ON_EXIT
    removed: list[str] = []
    for name in targets:
        p = OPENALGO_DIR / name
        if not p.exists():
            continue
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            removed.append(name)
        except OSError as e:
            print(f"[WARN] could not scrub {p}: {e}")
    return removed


def run_upgrade_check(strict: bool) -> int:
    """Invoke scripts/upgrade_check.py as a subprocess and return its exit code.

    Doing it as a subprocess (rather than importing) means the pin logic
    stays in one place and the launcher never duplicates the fingerprint
    code. The trade-off is one extra Python startup (~150 ms on this box),
    which is invisible next to OpenAlgo's own import cost.
    """
    script = ROOT / "scripts" / "verify" / "upgrade_check.py"
    if not script.is_file():
        print(f"[WARN] {script} missing - skipping upgrade check")
        return 0
    cmd = [sys.executable, str(script), "--strict", "--allow-unpinned"]
    rc = subprocess.call(cmd)
    if strict and rc != 0:
        return rc
    return 0


def main() -> int:
    args = parse_args()

    if not OPENALGO_DIR.is_dir():
        print(f"[FAIL] openalgo folder not found at {OPENALGO_DIR}")
        return 1

    # Bridge _restored/ state into the pristine clone BEFORE importing app,
    # because app.py binds its db paths and .env at import time.
    if not args.no_restore:
        rc = bootstrap_restored(force=args.force_restore)
        if rc != 0:
            return rc

    # Upgrade check: print a one-line diff of the watched files (cheap), and
    # refuse to start if --strict-pin was passed and the pin does not match.
    rc = run_upgrade_check(strict=args.strict_pin)
    if rc != 0:
        return rc

    if args.check_only:
        print("[OpenAlgo] --check-only: bootstrap + upgrade check done, exiting.")
        scrub_secrets()  # leave no secret behind when only checking
        return 0

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
        if not args.no_scrub_on_exit:
            removed = scrub_secrets()
            if removed:
                print(f"[OpenAlgo] Scrubbed from openalgo/ on exit: {', '.join(removed)}")
                print("             (will be re-bootstrapped from _restored/ on next start)")
    return 0


if __name__ == "__main__":
    sys.exit(main())