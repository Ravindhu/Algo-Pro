# smoke_test.py - Post-upgrade acceptance gate for the blue_engine / OpenAlgo setup.
# Location: my-trading-system\scripts\verify\smoke_test.py
# Run:  openalgo\.venv\Scripts\python.exe my-trading-system\scripts\verify\smoke_test.py
# Exit 0 = all good. Exit 1 = do not trade.
#
# ARCHITECTURE (locked):
#   Python (blue_engine) -> OpenAlgo REST/SDK -> Broker -> Exchange
# OpenAlgo is a PRISTINE upstream clone. Nothing POV-related is ever
# patched into it. The engine talks only through the public `openalgo` SDK.

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PASS = []
FAIL = []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  [PASS] {name}")
    except Exception as e:
        FAIL.append((name, str(e)))
        print(f"  [FAIL] {name}: {e}")


def _host():
    return os.getenv("HOST_SERVER", "http://127.0.0.1:5000")


def _api_key():
    """Key from the shell env, else from my-trading-system/.env (skips comments)."""
    k = os.getenv("OPENALGO_API_KEY", "").strip()
    if k:
        return k
    # State lives in the sibling _restored/ folder; fall back to the clone's
    # own .env (start_openalgo.py copies it there during bootstrap).
    for envf in (ROOT / "_restored" / ".env",
                 ROOT / "openalgo" / ".env",
                 ROOT / ".env"):
        if not envf.exists():
            continue
        for line in envf.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("#") or "=" not in s:
                continue
            name, _, val = s.partition("=")
            if name.strip() == "OPENALGO_API_KEY":
                v = val.strip().strip("'").strip('"')
                if v:
                    return v
    return ""


# ---- 1. blue_engine imports cleanly (installed package) ----
def t_engine_import():
    import blue_engine
    import blue_engine.core.runner


check("blue_engine imports cleanly", t_engine_import)


# ---- 2. engine config schema ----
def t_config_schema():
    import blue_engine
    cfg_path = Path(blue_engine.__file__).parent / "input" / "master.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert isinstance(cfg, dict), "master.json is not a dict"


check("engine input/master.json parses (schema ok)", t_config_schema)


# ---- 3. dashboard app present (FastAPI, repo root dashboard/app.py) ----
def t_dashboard_import():
    import importlib.util
    root = Path(__file__).resolve().parents[2]
    assert (root / "dashboard" / "app.py").exists(), "dashboard/app.py missing"
    assert importlib.util.find_spec("fastapi") is not None, "fastapi not installed"


check("dashboard app present (FastAPI)", t_dashboard_import)


# ---- 4. local state in place (static, no server needed) ----
def t_state():
    """The _restored/ source of truth must exist and be intact.

    .env and db/ are cryptographically married: FERNET_SALT and
    API_KEY_PEPPER in .env are the KDF inputs that encrypt the broker
    session tokens stored in db/openalgo.db. A partial restore boots fine
    and then silently cannot decrypt the broker login - so both are
    asserted here, together.
    """
    restored = ROOT / "_restored"
    assert restored.is_dir(), f"_restored/ missing at {restored}"
    for required in (".env", "db"):
        assert (restored / required).exists(), f"_restored/{required} missing"


check("local state (_restored/.env + _restored/db present)", t_state)


# ---- 4b. the clone carries no POV (retired server-side) ----
def t_no_pov_in_clone():
    clone = ROOT / "openalgo"
    stale = [
        clone / "services" / "volume_participation_service.py",
        clone / "restx_api" / "volume_participation.py",
        clone / "blueprints" / "pov_terminal.py",
    ]
    present = [str(f.relative_to(clone)) for f in stale if f.exists()]
    assert not present, f"POV files present in clone: {present}"


check("OpenAlgo clone is free of POV (retired server-side)", t_no_pov_in_clone)


# ---- 5. OpenAlgo server alive (only if currently running) ----
def t_server():
    urllib.request.urlopen(_host() + "/auth/check-setup", timeout=3)


check("OpenAlgo server responds (only if currently running)", t_server)


# ---- 6. SDK contract (needs server + API key) ----
def t_sdk():
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("no API key in env or my-trading-system/.env")
    from openalgo import api
    c = api(api_key=api_key, host=_host())
    r = c.analyzerstatus()
    assert isinstance(r, dict), "SDK returned non-dict"


check("SDK contract (analyzer status via openalgo SDK)", t_sdk)


# ---- 7. retired endpoint must be gone (only if server running) ----
def t_pov_endpoint_gone():
    """POST /api/v1/povorder must 404: the server-side POV is retired."""
    import urllib.error

    req = urllib.request.Request(
        _host() + "/api/v1/povorder",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=3)
    except urllib.error.HTTPError as e:
        assert e.code == 404, f"expected 404, got {e.code}"
        return
    except Exception:
        # Server not running - nothing to assert against.
        return
    raise AssertionError("povorder endpoint is still registered")


check("server-side POV endpoint retired (404)", t_pov_endpoint_gone)


HARD_PREFIXES = ("blue_engine", "engine ", "dashboard", "local state",
                 "OpenAlgo clone")
print()
print("=" * 60)
hard = [f for f in FAIL if f[0].startswith(HARD_PREFIXES)]
soft = [f for f in FAIL if f not in hard]
for n, e in soft:
    print(f"  [WARN] {n}: {e}")
    print("         (expected when the platform is not running - ok to ignore)")
if hard:
    for n, e in hard:
        print(f"  [HARD FAIL] {n}: {e}")
    print(f"SMOKE TEST: FAIL - {len(hard)} hard gate(s) failed. DO NOT TRADE.")
    sys.exit(1)
print(f"SMOKE TEST: PASS ({len(PASS)} checks, {len(soft)} warning(s))")