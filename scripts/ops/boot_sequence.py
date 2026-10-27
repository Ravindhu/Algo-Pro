r"""
boot_sequence.py - start the stack, WAIT for it, then log in. One scheduled job.

WHY THIS FILE EXISTS
====================
The audit finding was real: a 08:50 login task can fire before OpenAlgo is up,
fail leg 1, and burn a broker login attempt for nothing. ``scheduled_login.py``
refuses to start OpenAlgo by design (a login task must not be a supervisor),
and ``start_all.py`` refuses to log in by design (a launcher must not touch
credentials). Neither is wrong - but the *ordering* has to live somewhere.

It lives here. One process, one log, one exit code:

    1. probe OpenAlgo              reachable?  -> skip to 3
    2. start the stack             start_all.py --force
    3. wait until it ANSWERS        /auth/session-status returns 200
    4. log in                      scheduled_login.py --enable
    5. verify                      /auth/session-status authenticated=true

THE ONE RULE THIS FILE ENFORCES
-------------------------------
**Never log in against a server that is not ready.** Step 3 has its own timeout
and is checked with the endpoint that actually reports readiness, not with a
TCP connect (OpenAlgo binds the port ~200 s before it serves the app on a cold
boot on this box - a socket check would pass far too early).

WHY NOT JUST CHAIN THEM IN THE .CMD FILE
----------------------------------------
Because a .cmd file cannot tell the difference between "start_all timed out"
and "start_all succeeded", cannot retry readiness with backoff, and has no way
to report *which* of the two things failed. This script returns a distinct exit
code per failure, so the scheduler's "last result" is diagnostic.

EXIT CODES
----------
0   logged in, session verified
10  stack could not be started / OpenAlgo never answered
11  stack is up but login failed (see the login leg in the output)
2   not opted in - nothing was started (checked FIRST, before any side effect)
3   precondition problem (venv missing, scripts missing)

USAGE
-----
    python scripts\ops\boot_sequence.py                  # full sequence
    python scripts\ops\boot_sequence.py --check          # readiness only, no login
    python scripts\ops\boot_sequence.py --enable         # opt in for this run
    python scripts\ops\boot_sequence.py --no-start       # never start the stack
    python scripts\ops\boot_sequence.py --skip-login     # start + wait only

    python scripts\ops\boot_sequence.py --json           # machine-readable
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RUN_DIR = ROOT / "Temp" / "run"
BOOT_LOG = RUN_DIR / "boot_sequence.json"
START_ALL = ROOT / "scripts" / "ops" / "start_all.py"
SCHEDULED_LOGIN = ROOT / "scripts" / "ops" / "scheduled_login.py"
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"

EXIT_OK = 0
EXIT_NOT_OPTED_IN = 2
EXIT_PRECONDITION = 3
EXIT_STACK_FAILED = 10
EXIT_LOGIN_FAILED = 11

#: Readiness endpoint. ``/auth/session-status`` is served by Flask itself, so a
#: 200 means "the app is answering", not "the socket is bound".
READINESS_PATH = "/auth/session-status"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _say(msg: str) -> None:
    print(msg, flush=True)


def _write_record(payload: Dict[str, Any]) -> None:
    """Best-effort record. A logging failure must not change the outcome."""
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        BOOT_LOG.write_text(json.dumps(payload, indent=2, default=str) + "\n",
                            encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        _say(f"WARN: could not write {BOOT_LOG.name}: {exc}")


# --------------------------------------------------------------------------- #
# Readiness (not liveness)
# --------------------------------------------------------------------------- #
def probed(base_url: str, timeout: float = 5.0) -> Dict[str, Any]:
    """One readiness probe against ``READINESS_PATH``.

    Returns ``{"ready": bool, "status": int, "body": dict, "detail": str}``.
    A TCP connect is NOT enough: OpenAlgo binds the port long before Flask
    serves this route.
    """
    url = base_url.rstrip("/") + READINESS_PATH
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read(4096)
            status = int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        return {"ready": False, "status": int(exc.code), "body": {},
                "detail": f"HTTP {exc.code} from readiness endpoint"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ready": False, "status": 0, "body": {},
                "detail": f"not answering: {type(exc).__name__}"}
    try:
        body = json.loads(raw.decode("utf-8", errors="replace")) if raw.strip() else {}
    except json.JSONDecodeError:
        body = {}
    if status != 200 or not isinstance(body, dict):
        return {"ready": False, "status": status, "body": {},
                "detail": f"HTTP {status}, non-JSON body"}
    return {"ready": True, "status": status, "body": body,
            "detail": "readiness endpoint answered"}


def wait_until_ready(base_url: str, timeout: float = 300.0,
                     poll: float = 5.0) -> Dict[str, Any]:
    """Poll readiness until it answers or ``timeout`` expires.

    Deliberately slow at the start and steady after: a cold OpenAlgo boot on
    this box takes ~200 s, and hammering it every 200 ms changes nothing except
    the log volume.
    """
    started = time.monotonic()
    attempts = 0
    last: Dict[str, Any] = {"ready": False, "detail": "not probed"}
    while time.monotonic() - started < timeout:
        attempts += 1
        last = probed(base_url)
        if last["ready"]:
            return {**last, "attempts": attempts,
                    "waited_seconds": round(time.monotonic() - started, 1)}
        # Back off gently: 2 s, 3 s, then steady at ``poll``.
        time.sleep(min(2.0 + attempts, poll))
    return {**last, "attempts": attempts,
            "waited_seconds": round(time.monotonic() - started, 1),
            "timed_out": True}


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #
def _run(cmd: list, timeout: float) -> Dict[str, Any]:
    """Run a child script, capturing output. Never raises."""
    try:
        completed = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "output": f"timed out after {timeout}s"}
    except OSError as exc:
        return {"ok": False, "code": -1, "output": f"{type(exc).__name__}: {exc}"}
    output = (completed.stdout or "") + (completed.stderr or "")
    return {"ok": completed.returncode == 0, "code": completed.returncode,
            "output": output.strip()[-4000:]}


def start_stack(force: bool = True) -> Dict[str, Any]:
    """Start the whole stack via the existing launcher (single source of truth)."""
    cmd = [str(VENV_PY), str(START_ALL)]
    if force:
        cmd.append("--force")
    return _run(cmd, timeout=900)


def login(base_url: Optional[str] = None, enable: bool = True) -> Dict[str, Any]:
    """Run the login script and decode its exit code."""
    cmd = [str(VENV_PY), str(SCHEDULED_LOGIN)]
    if enable:
        cmd.append("--enable")
    if base_url:
        cmd += ["--url", base_url]
    return _run(cmd, timeout=180)


# --------------------------------------------------------------------------- #
# The sequence
# --------------------------------------------------------------------------- #
def run_sequence(*, base_url: str = "http://127.0.0.1:5000",
                 start: bool = True, login_after: bool = True,
                 enable: Optional[bool] = None,
                 readiness_timeout: float = 300.0) -> Dict[str, Any]:
    """Start (if needed) -> wait -> login -> verify. Returns a JSON-safe record."""
    from blue_engine.core.broker_auth import autologin_enabled

    record: Dict[str, Any] = {
        "started_at": _now(), "motive": "boot_sequence", "base_url": base_url,
        "steps": [], "ok": False, "skipped": False, "exit_code": EXIT_OK,
        "reason": "",
    }

    def step(name: str, ok: bool, detail: str = "") -> None:
        record["steps"].append({"step": name, "ok": bool(ok), "detail": detail,
                                "at": _now()})

    # ---- 0. preconditions + opt-in BEFORE any side effect ---------------- #
    if not VENV_PY.is_file() or not SCHEDULED_LOGIN.is_file():
        step("precondition", False, f"missing {VENV_PY.name} or scripts")
        record.update(reason="precondition failed",
                      exit_code=EXIT_PRECONDITION)
        return record

    opted_in = autologin_enabled() if enable is None else bool(enable)
    if login_after and not opted_in:
        # Checked FIRST so a scheduled task cannot start the trading stack as a
        # side effect of a login it was never allowed to perform.
        step("opt_in", False, "BROKER_AUTOLOGIN not enabled")
        record.update(skipped=True, reason="not opted in - nothing was started",
                      exit_code=EXIT_NOT_OPTED_IN)
        return record
    step("opt_in", True, "login allowed for this run")

    # ---- 1. is the stack already up? -------------------------------------- #
    ready = probed(base_url)
    step("probe", ready["ready"], ready["detail"])

    # ---- 2. start it if not ---------------------------------------------- #
    if not ready["ready"]:
        if not start:
            step("start", False, "--no-start: refusing to launch anything")
            record.update(reason="stack down and --no-start set",
                          exit_code=EXIT_STACK_FAILED)
            return record
        _say("stack is down - starting it (this can take ~200s)")
        launched = start_stack(force=True)
        step("start", launched["ok"],
             f"start_all exit={launched['code']}"
             + ("" if launched["ok"] else f"; {launched['output'][-500:]}"))

    # ---- 3. wait for READINESS, not for a socket -------------------------- #
    ready = wait_until_ready(base_url, timeout=readiness_timeout)
    step("readiness", ready["ready"],
         f"{ready['detail']} after {ready.get('waited_seconds')}s "
         f"({ready.get('attempts')} probes)")
    if not ready["ready"]:
        record.update(reason="OpenAlgo never became ready",
                      exit_code=EXIT_STACK_FAILED)
        return record

    # ---- 4. already authenticated? then there is nothing to do ----------- #
    body = ready.get("body") or {}
    if body.get("authenticated") is True:
        step("login", True,
             f"already authenticated as broker={body.get('broker') or '-'}; "
             f"no login performed")
        record.update(ok=True, exit_code=EXIT_OK,
                      reason="session already valid")
        return record

    if not login_after:
        step("login", True, "skipped (--skip-login)")
        record.update(ok=True, exit_code=EXIT_OK, reason="stack ready; login skipped")
        return record

    # ---- 5. log in -------------------------------------------------------- #
    logged = login(base_url, enable=True)
    step("login", logged["ok"],
         f"scheduled_login exit={logged['code']}")
    record["login_output"] = logged["output"][-2000:]
    if not logged["ok"]:
        record.update(reason="login failed", exit_code=EXIT_LOGIN_FAILED)
        return record

    # ---- 6. verify independently ----------------------------------------- #
    after = probed(base_url)
    authenticated = (after.get("body") or {}).get("authenticated") is True
    step("verify", authenticated,
         f"authenticated={authenticated} "
         f"broker={(after.get('body') or {}).get('broker') or '-'}")
    if not authenticated:
        record.update(reason="login reported success but the session is not "
                             "authenticated", exit_code=EXIT_LOGIN_FAILED)
        return record

    record.update(ok=True, exit_code=EXIT_OK, reason="logged in and verified")
    return record


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="boot_sequence.py",
        description="Start the stack, wait for readiness, then log in.")
    parser.add_argument("--url", default=None, help="OpenAlgo base URL")
    parser.add_argument("--no-start", action="store_true",
                        help="never start the stack; require it to be up")
    parser.add_argument("--skip-login", action="store_true",
                        help="start and wait only")
    parser.add_argument("--check", action="store_true",
                        help="readiness check only; starts nothing, logs in "
                             "nowhere")
    parser.add_argument("--enable", action="store_true",
                        help="opt in for this run")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="seconds to wait for readiness (default 300)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from blue_engine.economics.providers.secrets import get_secret

    base_url = args.url or get_secret("OPENALGO_URL") or "http://127.0.0.1:5000"

    if args.check:
        ready = probed(base_url)
        payload = {"mode": "check", "base_url": base_url, **ready, "at": _now()}
        _write_record(payload)
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            print(f"readiness: {'READY' if ready['ready'] else 'NOT READY'}")
            print(f"  url   : {base_url}{READINESS_PATH}")
            print(f"  detail: {ready['detail']}")
            body = ready.get("body") or {}
            if body:
                print(f"  auth  : authenticated={body.get('authenticated')} "
                      f"broker={body.get('broker') or '-'}")
        return EXIT_OK if ready["ready"] else EXIT_STACK_FAILED

    record = run_sequence(base_url=base_url, start=not args.no_start,
                          login_after=not args.skip_login,
                          enable=True if args.enable else None,
                          readiness_timeout=args.timeout)
    record["finished_at"] = _now()
    _write_record(record)

    if args.json:
        print(json.dumps(record, indent=2, default=str))
    else:
        print("=" * 62)
        print("BLUE ENGINE - boot sequence")
        print("=" * 62)
        for row in record["steps"]:
            print(f"  [{'OK  ' if row['ok'] else 'FAIL'}] "
                  f"{row['step']}: {row['detail']}")
        print("-" * 62)
        print(f"{'OK' if record['ok'] else 'FAILED'}: {record['reason']}")
    return int(record.get("exit_code", EXIT_OK))


if __name__ == "__main__":
    sys.exit(main())
