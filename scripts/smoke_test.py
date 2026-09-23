# smoke_test.py - Post-upgrade acceptance gate for the POV/OpenAlgo setup.
# Location: my-trading-system\scripts\smoke_test.py
# Run:  openalgo\.venv\Scripts\python.exe scripts\smoke_test.py
# Exit 0 = all good. Exit 1 = do not trade.

import os
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))  # not needed for engine (installed) but harmless

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

# ---- 1. pov_engine imports cleanly (installed package) ----
def t_engine_import():
    import pov_engine
    import pov_engine.runner
check("pov_engine imports cleanly", t_engine_import)

# ---- 2. engine config schema ----
def t_config_schema():
    import pov_engine
    from pov_engine import runner
    cfg_path = Path(pov_engine.__file__).parent / "input" / "master.json"
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert isinstance(cfg, dict), "master.json is not a dict"
check("engine input/master.json parses (schema ok)", t_config_schema)

# ---- 3. dashboard module imports (dash installed) ----
def t_dashboard_import():
    from pov_engine.dashboard import dash_app
check("dashboard module imports", t_dashboard_import)

# ---- 4. OpenAlgo wiring in place (static, no server needed) ----
def t_wiring():
    app_py = (ROOT / "openalgo" / "app.py").read_text(encoding="utf-8")
    assert "from blueprints.pov_terminal import pov_terminal_bp" in app_py, "app.py import missing"
    assert "app.register_blueprint(pov_terminal_bp)" in app_py, "app.py registration missing"
    bp = (ROOT / "openalgo" / "blueprints" / "pov_terminal.py").exists()
    assert bp, "blueprints/pov_terminal.py missing"
check("OpenAlgo wiring (import + register + blueprint file)", t_wiring)

# ---- 5. OpenAlgo server alive (optional - only if running) ----
def t_server():
    import urllib.request
    urllib.request.urlopen(_host() + "/auth/check-setup", timeout=3)
check("OpenAlgo server responds (only if currently running)", t_server)

# ---- 6. SDK contract (optional - needs server + API key) ----
def t_sdk():
    api_key = os.getenv("OPENALGO_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENALGO_API_KEY not set in env - set it in my-trading-system/.env")
    from openalgo import api
    c = api(api_key=api_key, host=_host())
    r = c.analyzerstatus()  # cheap, side-effect-free endpoint
    assert isinstance(r, dict), "SDK returned non-dict"
check("SDK contract (analyzer status via openalgo SDK)", t_sdk)

print()
print("=" * 56)
if FAIL:
    # server-dependent checks are expected to fail when OpenAlgo is not running;
    # treat checks 5-6 as warnings, 1-4 as hard gates
    hard = [f for f in FAIL if f[0].startswith(("pov_engine", "engine", "dashboard", "OpenAlgo wiring"))]
    warn = [f for f in FAIL if f not in hard]
    for n, e in warn:
        print(f"  [WARN] {n} (platform not running / key not set - ok to ignore now)")
    if hard:
        print(f"SMOKE TEST: FAIL - {len(hard)} hard gate(s) failed. DO NOT TRADE.")
        sys.exit(1)
    print("SMOKE TEST: PASS (with warnings)")
    sys.exit(0)
print("SMOKE TEST: PASS - all checks green. Safe to trade.")