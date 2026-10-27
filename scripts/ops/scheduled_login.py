r"""
scheduled_login.py - headless OpenAlgo + broker login for Task Scheduler / cron.

WHY THIS SCRIPT EXISTS
======================
The desk needs a broker session before the trading window opens, and the
session does not survive a broker-side daily rollover. Doing it by hand at
08:55 is a single point of failure; doing it in a loop inside the engine would
mean the engine can lock itself out with wrong credentials.

So login lives HERE: one short, non-interactive run that either succeeds and
exits 0, or fails and exits non-zero with a reason Task Scheduler can record.

WHAT IT DOES
------------
1. Loads the five credentials from ``.env`` (names only in any output).
2. Preflight: credentials present, TOTP seed valid base32, OpenAlgo reachable.
3. Leg 1 - OpenAlgo platform login (password, plus the account's own TOTP if
   that account has 2FA enabled).
4. Leg 2 - broker login with a pyotp code for ``BROKER_TOTP_SECRET``.
5. Verifies by probing an authenticated endpoint - not by trusting a login 200.
6. Writes ``Temp/run/last_login.json`` and appends ``Temp/run/login_history.jsonl``.

WHAT IT REFUSES TO DO
---------------------
* It will not run unless ``BROKER_AUTOLOGIN=1``.
* It never starts, stops or restarts OpenAlgo. If OpenAlgo is down, the exit
  code says so and the launcher (start_all.py) is the operator's call.
* It never trades, never edits ``openalgo/``, never writes config.
* It never logs a password, a TOTP seed or a 6-digit code.

EXIT CODES
----------
0  session ready (both legs + verify)
2  not opted in (BROKER_AUTOLOGIN is not 1)
3  configuration problem (missing credentials, unusable TOTP seed)
4  OpenAlgo not reachable
5  platform login failed
6  broker login failed
7  login responded but the session probe failed
8  refused by the login cooldown (too soon after the last attempt)

COOLDOWN (why exit 8 exists)
-----------------------------
A broker locks an account for repeated logins, and OpenAlgo rate-limits
``/auth/login`` to 5/minute and 25/hour per IP. So a real attempt is never
made faster than 10 minutes after a success, or 30 minutes after a failure -
regardless of how many times Task Scheduler restarts the task. ``--force``
is the only way past it, and it is for a human at a terminal.

USAGE
-----
    python scripts\ops\scheduled_login.py            # run (needs opt-in)
    python scripts\ops\scheduled_login.py --status   # report, never logs in
    python scripts\ops\scheduled_login.py --check    # preflight only, no login
    python scripts\ops\scheduled_login.py --enable   # opt in for THIS run
    python scripts\ops\scheduled_login.py --no-verify

TASK SCHEDULER (Weekdays 08:50, before the 09:15 open)
------------------------------------------------------
    schtasks /Create /TN "BlueEngine\BrokerLogin" /SC WEEKLY /D MON,TUE,WED,THU,FRI ^
      /ST 08:50 /TR "C:\...\scripts\ops\SCHEDULED_LOGIN.cmd" /RL LIMITED /F

Or import ``scripts\ops\broker_login_task.xml`` (ships with a 08:50 trigger,
"run whether user is logged on or not", and a 3x restart-on-failure policy).

CRON (Linux / WSL)
------------------
    50 8 * * 1-5  cd /path/my-trading-system && .venv/bin/python scripts/ops/scheduled_login.py >> Temp/run/login_cron.log 2>&1

ONE RULE FOR THE SCHEDULE: a clock-time trigger is safer than a boot trigger.
Logging in before OpenAlgo is up fails leg 1 anyway, and a boot-time run can
land outside market hours with a token that expires before the open.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RUN_DIR = ROOT / "Temp" / "run"
LAST_LOGIN = RUN_DIR / "last_login.json"
HISTORY = RUN_DIR / "login_history.jsonl"

EXIT_OK = 0
EXIT_NOT_OPTED_IN = 2
EXIT_CONFIG = 3
EXIT_UNREACHABLE = 4
EXIT_PLATFORM = 5
EXIT_BROKER = 6
EXIT_VERIFY = 7
EXIT_COOLDOWN = 8


def _write_run_record(payload: Dict[str, Any]) -> None:
    """Persist a small, secret-free record. Best effort - never fails the run."""
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        LAST_LOGIN.write_text(encoded + "\n", encoding="utf-8")
        with HISTORY.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False,
                                    default=str) + "\n")
    except OSError as exc:  # noqa: BLE001 - a log failure must not mask a login
        print(f"WARN: could not write run record: {exc}", file=sys.stderr)


def _exit_code_for(leg: str, reason: str) -> int:
    """Map the failing leg to a stable exit code."""
    text = f"{leg} {reason}".lower()
    if "missing credentials" in text or "totp secret is unusable" in text:
        return EXIT_CONFIG
    if "not reachable" in text or "unreachable" in text:
        return EXIT_UNREACHABLE
    if "session probe" in text:
        return EXIT_VERIFY
    if leg == "broker":
        return EXIT_BROKER
    if leg == "openalgo":
        return EXIT_PLATFORM
    return EXIT_CONFIG


def _print_human(payload: Dict[str, Any]) -> None:
    print("=" * 62)
    print("BLUE ENGINE - scheduled broker login")
    print("=" * 62)
    if payload.get("skipped"):
        print(f"SKIPPED: {payload.get('reason')}")
        print("Set BROKER_AUTOLOGIN=1 in .env to enable headless login,")
        print("or pass --enable for a one-off run.")
        return
    for step in payload.get("steps") or []:
        mark = "OK  " if step.get("ok") else "FAIL"
        detail = step.get("detail") or ""
        print(f"  [{mark}] {step.get('step')}: {detail}")
    print("-" * 62)
    if payload.get("ok"):
        print(f"SESSION READY  broker={payload.get('broker') or '-'}  "
              f"({payload.get('duration_seconds')}s)")
    else:
        print(f"FAILED at leg '{payload.get('leg')}': {payload.get('reason')}")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scheduled_login.py",
        description="Headless OpenAlgo + broker TOTP login. Never trades.")
    parser.add_argument("--status", action="store_true",
                        help="report the last run and credential presence; "
                             "never logs in")
    parser.add_argument("--check", action="store_true",
                        help="preflight only (credentials + TOTP seed + "
                             "reachability); never logs in")
    parser.add_argument("--enable", action="store_true",
                        help="opt in for this run even if BROKER_AUTOLOGIN=0")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip the post-login session probe (not advised)")
    parser.add_argument("--force", action="store_true",
                        help="ignore the login cooldown. A HUMAN decision only "
                             "- repeated logins can lock the broker account")
    parser.add_argument("--url", default=None, help="OpenAlgo base URL")
    parser.add_argument("--json", action="store_true", help="machine-readable")
    args = parser.parse_args(argv)

    from blue_engine.core import broker_auth
    from blue_engine.economics.providers.secrets import get_secret

    if args.status:
        credentials = broker_auth.load_credentials()
        last: Optional[Dict[str, Any]] = None
        if LAST_LOGIN.is_file():
            try:
                last = json.loads(LAST_LOGIN.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                last = {"error": "last_login.json is unreadable"}
        payload = {
            "autologin_enabled": broker_auth.autologin_enabled(),
            "credentials": credentials.describe(),
            "missing": credentials.missing_required(),
            "totp_seed": broker_auth.verify_totp_secret(
                credentials.broker_totp_secret),
            "last_login": last,
        }
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            print("autologin enabled :", payload["autologin_enabled"])
            print("missing           :", ", ".join(payload["missing"]) or "none")
            for name, value in payload["credentials"].items():
                print(f"  {name:24s} {value}")
            print("totp seed         :", payload["totp_seed"]["reason"])
            guard = broker_auth.login_guard()
            print("login allowed now :", guard["allowed"], "|", guard["reason"])
            if last:
                if last.get("cooldown"):
                    print("last login        : SKIPPED (cooldown) |",
                          last.get("at"), "|", last.get("reason") or "")
                else:
                    print("last login        :",
                          "OK" if last.get("ok") else "FAILED",
                          "|", last.get("finished_at") or last.get("at"),
                          "|", last.get("reason") or "")
        return EXIT_OK

    url = args.url or get_secret("OPENALGO_URL") or "http://127.0.0.1:5000"
    auth = broker_auth.BrokerAuth(url)

    if args.check:
        result = auth.preflight()
        payload = result.to_dict()
        payload["skipped"] = False
        _write_run_record({**payload, "mode": "preflight",
                           "at": datetime.now(timezone.utc).isoformat()})
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            _print_human(payload)
        return EXIT_OK if result.ok else _exit_code_for(result.leg, result.reason)

    opted_in = args.enable or broker_auth.autologin_enabled()
    if not opted_in:
        payload = {"ok": False, "skipped": True, "leg": "",
                   "reason": "BROKER_AUTOLOGIN is not enabled",
                   "at": datetime.now(timezone.utc).isoformat()}
        _write_run_record(payload)
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            _print_human(payload)
        return EXIT_NOT_OPTED_IN

    # ---- COOLDOWN: refuse BEFORE any broker request -----------------------
    # Task Scheduler's restart-on-failure plus a curious operator is how an
    # account gets locked. The guard is independent of --enable: opting in
    # grants permission for A login, not for unlimited logins.
    guard = broker_auth.login_guard()
    if not guard["allowed"] and not args.force:
        payload = {"ok": False, "skipped": True, "cooldown": True,
                   "leg": "cooldown", "reason": guard["reason"],
                   "wait_seconds": guard["wait_seconds"],
                   "last_attempt_at": guard.get("last_attempt_at"),
                   "mode": "login",
                   "at": datetime.now(timezone.utc).isoformat()}
        _write_run_record(payload)
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            print("=" * 62)
            print("BLUE ENGINE - scheduled broker login")
            print("=" * 62)
            print(f"  [SKIP] {guard['reason']}")
            print("  No broker attempt was made. Retrying now risks an account lock.")
        return EXIT_COOLDOWN

    result = auth.run(verify=not args.no_verify)
    # Stamp the attempt here too: this script does not go through
    # broker_auth.run_scheduled_login(), so without this the guard would never
    # see the logins it is meant to limit.
    broker_auth.record_login_attempt(ok=bool(result.ok),
                                     outcome=(result.reason or "attempt completed"))
    payload = result.to_dict()
    payload["skipped"] = False
    payload["mode"] = "login"
    payload["at"] = datetime.now(timezone.utc).isoformat()
    _write_run_record(payload)

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        _print_human(payload)
    return EXIT_OK if result.ok else _exit_code_for(result.leg, result.reason)


if __name__ == "__main__":
    sys.exit(main())
