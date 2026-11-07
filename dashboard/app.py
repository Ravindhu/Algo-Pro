"""FastAPI dashboard. Reads the paper ledger, audit log and kill switch. Never sends orders.

Writes (all need the DASHBOARD_API_TOKEN header, decision 0020):
    POST /api/limits       save user-set risk limits
    POST /api/kill/trip    block new entries (reason required, audited)
    POST /api/kill/reset   clear the kill switch (reason required, audited)
Reads: GET /api/overview, /api/positions, /api/events, /api/health, /api/kill.

Run (after installing requirements, on the owner's machine):
    python dashboard/app.py            # serves http://127.0.0.1:8050
Flow stage: Dashboard
Status: PHASE 3 IMPLEMENTED (paper data only; FastAPI imported at run time)
Data logic: src/quant/reporting/dashboard_data.py
"""
# No `from __future__ import annotations` here: FastAPI reads route annotations
# from module globals, and `Request` is a local name inside create_app.
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from quant.audit.log import AuditLog  # noqa: E402
from quant.core.config import RiskLimits  # noqa: E402
from quant.core.env import load_env_file  # noqa: E402
from quant.core.limits_store import apply_limit_updates  # noqa: E402
from quant.monitor.ledger import Ledger  # noqa: E402
from quant.reporting.dashboard_data import build_overview  # noqa: E402
from quant.core.setup_store import apply_setup_updates, read_setup  # noqa: E402
from quant.risk.kill_switch import KillSwitch  # noqa: E402

TOKEN_ENV = "DASHBOARD_API_TOKEN"
TOKEN_HEADER = "X-Dashboard-Token"
REASON_MAX = 200
ACTOR = "dashboard-api"

DEFAULT_PATHS = {
    "ledger": ROOT / "data" / "paper" / "ledger.jsonl",
    "audit": ROOT / "data" / "paper" / "audit.jsonl",
    "kill": ROOT / "data" / "paper" / "kill_switch.json",
    "limits": ROOT / "config" / "risk_limits.yaml",
    "settings": ROOT / "config" / "settings.yaml",
    "strategies": ROOT / "config" / "strategies",
    "page": ROOT / "src" / "quant" / "index.html",
    "flow": ROOT / "dashboard" / "assets" / "flow_svg.js",
}

API_INDEX = {
    "service": "Algo-Pro dashboard",
    "page": "/ui",
    "endpoints": [
        "/api/overview", "/api/positions", "/api/events", "/api/health", "/api/kill", "/api/setup",
        "POST /api/limits (token)", "POST /api/kill/trip (token)", "POST /api/kill/reset (token)",
        "POST /api/setup (token)",
    ],
}


def read_settings(path: Path) -> Dict[str, object]:
    """Dashboard section of settings.yaml. Missing file or key gives the defaults."""
    import yaml

    if not path.exists():
        return {"pnl_visible_default": True}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {"pnl_visible_default": True, **(data.get("dashboard") or {})}


def load_state(paths: Dict[str, Path], last_prices: Optional[Dict[str, float]] = None) -> Dict[str, object]:
    """Read every source once and build the overview. Missing files mean an empty, safe state."""
    ledger = Ledger(paths["ledger"])
    audit = AuditLog(paths["audit"])
    kill = json.loads(paths["kill"].read_text(encoding="utf-8")) if paths["kill"].exists() else {}
    limits = RiskLimits.from_yaml(paths["limits"]) if paths["limits"].exists() else RiskLimits()
    fills = [e for e in ledger.events() if e.get("event") == "fill"]
    overview = build_overview(
        fills=fills,
        audit_records=audit.read_all(),
        kill=kill,
        limits={k: getattr(limits, k) for k in limits.__dataclass_fields__},
        last_prices=last_prices or {},
    )
    overview["settings"] = read_settings(paths["settings"])
    overview["feed"] = {"status": "not connected", "note": "No live feed exists yet (paper data only)."}
    return overview


def create_app(paths: Optional[Dict[str, Path]] = None):
    """Build the FastAPI app. Import happens here so the module loads without FastAPI installed."""
    from fastapi import FastAPI, Request
    from fastapi.responses import FileResponse, JSONResponse

    load_env_file()  # reads the repo-root .env without overriding shell variables
    paths = {**DEFAULT_PATHS, **(paths or {})}
    app = FastAPI(title="Algo-Pro dashboard", version="0.1.0")

    def token_error(request: Request):
        """None when the token is good. Otherwise the error response (503 no server token, 401 wrong)."""
        expected = os.environ.get(TOKEN_ENV, "")
        if not expected:
            return JSONResponse({"error": f"writes disabled: {TOKEN_ENV} is not set"}, status_code=503)
        given = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return None

    async def reason_from(request: Request):
        """(reason, None) when valid, else (None, 422 response)."""
        try:
            body = await request.json()
        except ValueError:  # JSON errors are ValueError subclasses
            body = None
        reason = body.get("reason") if isinstance(body, dict) else None
        if not isinstance(reason, str) or not 0 < len(reason.strip()) <= REASON_MAX:
            return None, JSONResponse({"error": f"reason is required (1-{REASON_MAX} characters)"},
                                      status_code=422)
        return reason.strip(), None

    @app.get("/")
    def index():
        return JSONResponse(API_INDEX)

    @app.get("/ui")
    def ui():
        """The one page: reads /api/overview every 2 s, writes through the token-protected POST routes."""
        return FileResponse(paths["page"], media_type="text/html")

    @app.get("/assets/flow_svg.js")
    def flow_asset():
        return FileResponse(paths["flow"], media_type="application/javascript")

    @app.post("/api/limits")
    async def set_limits(request: Request):
        """Save user-set risk limits. Needs the DASHBOARD_API_TOKEN header. Refused if no token is set."""
        denied = token_error(request)
        if denied is not None:
            return denied
        try:
            result = apply_limit_updates(paths["limits"], await request.json())
        except ValueError as exc:  # JSON errors are ValueError subclasses too
            return JSONResponse({"error": str(exc)}, status_code=422)
        AuditLog(paths["audit"]).append("limits_changed", {**result, "actor": ACTOR})
        return JSONResponse({"changed": result})

    @app.get("/api/kill")
    def kill_status():
        switch = KillSwitch(paths["kill"])
        return JSONResponse({"active": switch.active, "reason": switch.reason})

    @app.post("/api/kill/trip")
    async def kill_trip(request: Request):
        """Block new entries. Open positions are closed by the paper runner, never by this API."""
        denied = token_error(request)
        if denied is not None:
            return denied
        reason, bad = await reason_from(request)
        if bad is not None:
            return bad
        switch = KillSwitch(paths["kill"])
        switch.trip(reason, source=ACTOR)
        AuditLog(paths["audit"]).append("kill_switch", {"action": "trip", "reason": reason, "actor": ACTOR})
        return JSONResponse({"active": switch.active, "reason": switch.reason})

    @app.post("/api/kill/reset")
    async def kill_reset(request: Request):
        """Clear the kill switch. The reset is written to the override log."""
        denied = token_error(request)
        if denied is not None:
            return denied
        reason, bad = await reason_from(request)
        if bad is not None:
            return bad
        switch = KillSwitch(paths["kill"])
        switch.reset(ACTOR, reason, audit=AuditLog(paths["audit"]))
        return JSONResponse({"active": switch.active, "reason": switch.reason})

    @app.get("/api/setup")
    def setup_status():
        return JSONResponse(read_setup(paths["settings"], paths["strategies"]))

    @app.post("/api/setup")
    async def setup_save(request: Request):
        """Save risk per trade, trades per day and strategy. Needs the token."""
        denied = token_error(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
            result = apply_setup_updates(paths["settings"], paths["strategies"], body)
        except ValueError as exc:  # JSON errors are ValueError subclasses too
            return JSONResponse({"error": str(exc)}, status_code=422)
        AuditLog(paths["audit"]).append("setup_changed", {**result, "actor": ACTOR})
        return JSONResponse({"changed": result})

    @app.get("/api/overview")
    def overview():
        return JSONResponse(load_state(paths))

    @app.get("/api/positions")
    def positions():
        return JSONResponse(load_state(paths)["positions"])

    @app.get("/api/events")
    def events():
        return JSONResponse(load_state(paths)["events"])

    @app.get("/api/health")
    def health():
        state = load_state(paths)
        return JSONResponse({"feed": state["feed"], "mode": state["mode"],
                             "kill_switch": state["kill_switch"]["active"]})

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8050)
