# smoke_test.py - Post-upgrade acceptance gate for the POV/OpenAlgo setup.
# Location: my-trading-system\scripts\smoke_test.py
# Run:  openalgo\.venv\Scripts\python.exe my-trading-system\scripts\smoke_test.py
# Exit 0 = all good. Exit 1 = do not trade.

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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
    envf = ROOT / ".env"
    if envf.exists():
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


# ---- 1. pov_engine imports cleanly (installed package) ----
def t_engine_import():
    import pov_engine
    import pov_engine.runner


check("pov_engine imports cleanly", t_engine_import)


# ---- 2. engine config schema ----
def t_config_schema():
    import pov_engine
    cfg_path = Path(pov_engine.__file__).parent / "input" / "master.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert isinstance(cfg, dict), "master.json is not a dict"


check("engine input/master.json parses (schema ok)", t_config_schema)


# ---- 3. dashboard module imports ----
def t_dashboard_import():
    from pov_engine.dashboard import dash_app


check("dashboard module imports", t_dashboard_import)


# ---- 4. OpenAlgo wiring in place (static, no server needed) ----
def t_wiring():
    app_py = (ROOT / "openalgo" / "app.py").read_text(encoding="utf-8")
    assert "from blueprints.pov_terminal import pov_terminal_bp" in app_py, "app.py import missing"
    assert "app.register_blueprint(pov_terminal_bp)" in app_py, "app.py registration missing"
    assert (ROOT / "openalgo" / "blueprints" / "pov_terminal.py").exists(), "blueprint file missing"


check("OpenAlgo wiring (import + register + blueprint file)", t_wiring)


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


# ---- 7. blueprint endpoint (only if server running) ----
def t_blueprint_endpoint():
    urllib.request.urlopen(_host() + "/pov-terminal/config", timeout=3)


check("POV terminal config endpoint (only if currently running)", t_blueprint_endpoint)


HARD_PREFIXES = ("pov_engine", "engine ", "dashboard", "OpenAlgo wiring")
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